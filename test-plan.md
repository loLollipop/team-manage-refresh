# PR #190 测试计划

## 背景
PR #190 在兑换页加了第三个 tab "续期申请"，复用既有 `/warranty/renewal-request` 端点；并修复了一个旧的 CSS bug —— 质保查询 tab 的输入框溢出遮挡下方 hint。

## 测试范围
端到端验证两类改动。所有测试在 `http://127.0.0.1:8000/`（本地 uvicorn）执行。

测试数据已预先 INSERT 到 SQLite：
- 质保码 `WTEST-WARRANTY-001`，`has_warranty=True`，已被 `tester@example.com` 用过（有 RedemptionRecord）
- 普通码 `WTEST-NORMAL-001`，`has_warranty=False`，已被 `tester@example.com` 用过

## 引用代码
- `app/static/css/user.css:435-451`（CSS fix：`align-items: end`，删除 `height: 100%` overrides）
- `app/templates/user/redeem.html:52-54, 137-176`（新 tab + 新 panel）
- `app/static/js/redeem.js:576-609`（switchTopTab 三 tab 支持）
- `app/static/js/redeem.js:1286-1322`（submitProactiveRenewal）
- `app/services/warranty.py:250-264`（错误文案：非质保码不支持续期申请 / 该邮箱未使用过此兑换码，无法申请续期）

---

## Test 1: 质保查询 tab 输入框不再遮挡 hint（CSS fix）
1. 打开 `http://127.0.0.1:8000/`
2. 点击顶部 "质保查询" tab
3. 截图整个 panel

**Pass criteria**:
- 输入框（placeholder "请输入原兑换码或邮箱进行查询"）的下边缘 + 灰色 hint 文字 "推荐用邮箱查询，可同时看兑换记录与质保状态。" **完整可读**，不被输入框覆盖
- 输入框高度自然 48px，左侧搜索图标 + placeholder 都正常
- 与提交按钮 "查询质保状态" 在水平方向对齐

**Adversarial**: 如果 CSS fix 没生效，按钮和输入框会拉成同高、输入框溢出 ~16px 把 hint 顶部切掉（这就是用户截图里看到的现象）。Pass 表现为：hint 完整可读、输入框不变形。

---

## Test 2: 续期申请 tab 存在并可切换
1. 在兑换页观察顶部 tab bar
2. 点击 "续期申请" tab
3. 截图

**Pass criteria**:
- 顶部 tab 共 **三个**：自助上车 / 质保查询 / 续期申请
- 点击 "续期申请" 后，下半部分切换为 panel `<h2>续期申请</h2>`，表单里有 **两个** 输入框（邮箱 + 兑换码）和 **一个** 提交按钮 "提交续期申请"
- tab indicator（橙色滑动条）滑到 "续期申请" 下方

**Adversarial**: 如果 switchTopTab 没扩到 3 tab，点击会切失败 / panel 不显示 / indicator 不动。

---

## Test 3: 续期申请成功路径（主流程）
1. 在 "续期申请" tab 输入邮箱 `tester@example.com` + 兑换码 `WTEST-WARRANTY-001`
2. 点击 "提交续期申请"
3. 观察 toast

**Pass criteria**:
- toast 文本 = `续期申请已提交，请联系管理员尽快处理`
- toast 颜色 = success（左边绿色 border）
- 兑换码输入框被清空，邮箱输入框保留

**Adversarial**: 如果 submitProactiveRenewal 走错路径或服务层校验失败，toast 会变 error 颜色 / 文本不同。

---

## Test 4: 后台落库验证（用 sqlite，不走 admin UI）
执行 shell：
```
sqlite3 data/team_manage.db "SELECT id, email, code, status FROM renewal_requests;"
```

**Pass criteria**:
- 返回 1 行 = `(<id>, tester@example.com, WTEST-WARRANTY-001, pending)`

---

## Test 5: 非质保码被服务层拒绝（错误文案验证）
1. 在 "续期申请" tab 输入邮箱 `tester@example.com` + 兑换码 `WTEST-NORMAL-001`（已被 tester 用过的普通码）
2. 点击 "提交续期申请"

**Pass criteria**:
- toast 颜色 = error（左红 border）
- toast 文本 = `非质保码不支持续期申请`（**精确匹配**，由本 PR 改的服务层文案）

**Adversarial**: 如果服务层文案没改成新版本，会显示旧的 `该兑换码不是质保兑换码，无法申请续期`，本 step 立刻失败。

---

## Test 6: 邮箱不匹配被服务层拒绝
1. 在 "续期申请" tab 输入邮箱 `wrong@example.com`（**未用过此码**）+ 兑换码 `WTEST-WARRANTY-001`
2. 点击 "提交续期申请"

**Pass criteria**:
- toast 颜色 = error
- toast 文本 = `该邮箱未使用过此兑换码，无法申请续期`（**精确匹配**，由本 PR 改的服务层文案）

**Adversarial**: 如果服务层文案没改成新版本，会显示旧的 `该邮箱未使用该兑换码，无法申请续期`（区别在 "未使用过此" vs "未使用该"），本 step 失败。

---

## 不测的部分（明确说明）
- 管理员 `/admin/renewal-requests` UI 渲染该 pending 项 → 已在 service 层 `create_renewal_request` 单元测试中覆盖；UI 端只是 `select * from renewal_requests where status='pending'`，与 PR 改动无关
- 续期审批 → 累加 `extension_days` 的完整链路 → 与 PR 改动无关，PR #186 / #185 已覆盖
- IP 限流（60s 内 5+ 次）→ 跟本 PR 无关，沿用既有逻辑
