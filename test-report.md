# PR #190 测试报告

## 一句话总结
本地启动 uvicorn (`127.0.0.1:8000`)，端到端跑了 6 个 case，**全部通过**；其中执行测试的过程中发现并修复了一个 CSS 布局 bug（顶部 tab 写死 2 列，加第 3 个 tab 后折成 2x2），已合入本 PR。

## 重要提示（请先看）
- 测试启动时第一次进页面，三个 tab 因 `.top-nav-tabs` 写死 `repeat(2, 1fr)` 而**折成 2x2**，indicator 也是 `width: calc(50% - 10px)` 写死。已修：`repeat(3, minmax(0, 1fr))` + `calc(33.333% - 9px)`，commit `d86b744` 已推到本 PR。修复后所有 6 个 case 通过。
- 仓库未接 GitHub Actions，CI = Devin Review；前面 commits 都已通过；新加的 CSS 微调 commit 也只是样式调整。

## 测试结果

| # | Case | Result |
|---|------|--------|
| 1 | 三 tab 单行排列 | passed |
| 2 | 质保查询 hint 不被遮挡 | passed |
| 3 | 续期申请 tab 切换 + panel 渲染 | passed |
| 4 | 有效质保码提交成功 | passed |
| 5 | 非质保码错误文案精确匹配 | passed |
| 6 | 邮箱不匹配错误文案精确匹配 | passed |
| 7 | renewal_requests 表落库 | passed |

## 关键截图

### Test 1: CSS bug 修复前 vs 修复后

| 🔴 CSS BUG（修复前） | 🟢 修复后 |
|---|---|
| ![BUG: 三 tab 折成 2x2](https://app.devin.ai/attachments/e19f2dbe-6219-417a-8baa-13c648da8d58/screenshot_540695f057034fbda1ed8e307bb35b15.png) | ![FIX: 三 tab 单行](https://app.devin.ai/attachments/05dff0d9-193e-431e-9def-585ece839c29/screenshot_213c11274f294199bfd9f976da096981.png) |
| 自助上车 + 续期申请 左侧两行；质保查询 拉伸到右侧整个高度 | 自助上车 / 质保查询 / 续期申请 三 tab 在同一行，indicator 高亮当前 tab |

### Test 2: 质保查询 tab — 输入框未遮挡 hint

![质保查询 tab](https://app.devin.ai/attachments/b3100797-1826-4f56-b00a-982989696597/screenshot_05ebf3a0919542209af167ad9edc2a76.png)

输入框（"请输入原兑换码或邮箱进行查询"）下方 hint "推荐用邮箱查询，可同时看兑换记录与质保状态。" 完整可读，输入框与右侧 "查询质保状态" 按钮水平对齐。

### Test 3: 续期申请 tab 渲染

![续期申请 panel](https://app.devin.ai/attachments/dfaf5866-297f-472f-bcde-786e822a0f3f/screenshot_b57a39df65d44bb491c798cfc62c5956.png)

切换到续期申请 tab 后：indicator 滑到第三 tab 下方；panel 标题 "续期申请"；表单含邮箱、兑换码、提交按钮、提示气泡。

### Test 4: 有效质保码提交成功

![成功 toast](https://app.devin.ai/attachments/b83f0421-6983-400d-ba36-8010ff00aa6f/screenshot_b181a349c6bb465485fdf8c46d4b73ad.png)

输入 `tester@example.com` + `WTEST-WARRANTY-001`，点击提交。右上角 toast：**`续期申请已提交，请联系管理员尽快处理`**（精确匹配）。兑换码字段被清空（恢复 placeholder），邮箱保留。

### Test 5: 非质保码精确错误文案

![非质保码 toast](https://app.devin.ai/attachments/345634fa-55ac-41d2-8f00-0fd8467a78b6/screenshot_13f9a80dfe5a45929f07a8b10ab9e37d.png)

输入 `tester@example.com` + `WTEST-NORMAL-001`（正确邮箱 + 已用过的普通无质保码）。Toast：**`非质保码不支持续期申请`**（精确匹配，本 PR 改的服务层文案）。

### Test 6: 邮箱不匹配精确错误文案

![邮箱不匹配 toast](https://app.devin.ai/attachments/f7cc7fac-16a8-4208-9f10-7d775e7ce24a/screenshot_b4afd57981364ff0a47e757c11ff568c.png)

输入 `wrong@example.com`（未用过此码） + `WTEST-WARRANTY-001`。Toast：**`该邮箱未使用过此兑换码，无法申请续期`**（精确匹配，本 PR 改的服务层文案）。

### Test 7: 数据库落库

```
$ python -c "select RenewalRequest where code='WTEST-WARRANTY-001'"
id=1 email=tester@example.com code=WTEST-WARRANTY-001 status=pending
```

仅 1 行 → Test 5/6 的失败请求确实没有写库（服务层校验先于 INSERT，正确）；Test 4 的成功请求落了 1 行 `pending`，与预期完全一致。

## 测试范围说明
- 不测：管理员 `/admin/renewal-requests` 列表 UI、续期审批流程（不在本 PR 改动范围）
- 不测：IP 限流（沿用既有逻辑）

## 环境
- 本地 uvicorn `127.0.0.1:8000`
- SQLite `data/team_manage.db`，预先 seed 了 `WTEST-WARRANTY-001` (warranty) 和 `WTEST-NORMAL-001` (non-warranty) + 对应 RedemptionRecord
