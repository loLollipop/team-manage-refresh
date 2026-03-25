# GPT Team 车位管理与兑换系统

一个基于 FastAPI 的 ChatGPT Team 管理系统，围绕「账号导入 → 兑换分配 → 质保售后 → 库存预警」完整流程设计，适合需要稳定管理 Team 车位与兑换业务的场景。

## 🚀 Docker 一键部署 & 更新

### 一键部署
```bash
git clone https://github.com/loLollipop/team-manage-refresh.git
cd team-manage-refresh
cp .env.example .env
docker compose up -d
```

### 一键更新
```bash
git pull && docker compose down && docker compose up -d --build
```

### 覆盖旧项目（重写历史/强推后推荐）
```bash
git fetch origin
git checkout main
git reset --hard origin/main
docker compose down && docker compose up -d --build
```

## ⚠️ FastAPI/Starlette 兼容性说明（重要）

在新版 FastAPI/Starlette 环境中，若项目仍使用旧版 `templates.TemplateResponse` 调用方式，访问 `/login` 等页面可能报错：

`TypeError: unhashable type: 'dict'`

本项目已统一改为新版签名 `TemplateResponse(request, template_name, context)`，并已完成兼容性修复。

如果你之前拉取的是旧代码，请先更新到最新版本再部署：

```bash
git pull
```


## 🌟 二改亮点（相对原项目）

这个仓库不是“原模板直接复刻”，而是围绕实际运营场景做了持续二改，重点在稳定性、批量效率、售后追溯与自动化联动：

- **福利车位独立池 + 福利通用码机制**
  - 福利 Team 与常规 Team 分池管理，兑换逻辑隔离；福利通用码由 settings 驱动，并有影子码机制用于历史记录兼容。
- **兑换码批量管理能力完善**
  - 支持批量生成、批量修改质保、批量删除（仅删除无关联记录，防止误删历史）。
- **无效兑换码“先扫描后清理”**
  - 通过无效码扫描与清理流程，处理历史脏数据时可控且可回溯，避免直接硬删。
- **Token 生命周期自动化运维**
  - 内置 Token 预刷新任务 + Team 周期状态同步任务，并支持在后台动态配置参数。
- **库存预警与外部系统联动**
  - 库存阈值触发 Webhook，支持第三方系统自动导入 Team，减少人工补货延迟。
- **CliproxyAPI 推送对接**
  - 支持单个/批量将 Team 凭据推送到 CliproxyAPI，便于统一代理层消费。
- **售后/风控链路增强**
  - 提供质保查询、Device Code Auth 开启、封禁关联追踪等能力，方便售后定位与风险处理。
- **后台交互针对运营场景优化**
  - 设置页分区导航、批量操作浮动条、公告 Markdown 弹窗等，减少高频操作路径的点击成本。


### 管理员功能
- **Team 账号管理**
  - 单个/批量导入 Team 账号（支持 Access Token / Refresh Token / Session Token / Client ID）
  - 智能识别和提取 AT Token、邮箱、Account ID（支持从混合文本中提取）
  - 自动同步 Team 信息（名称、订阅计划、到期时间、成员数）
  - Team 成员管理（查看、添加、删除成员）
  - Team 状态监控（可用/已满/已过期/错误）
  - 支持 OAuth 授权链接生成与回调解析，便于获取和整理导入凭据

- **兑换码管理**
  - 单个/批量生成兑换码
  - 自定义兑换码和有效期
  - 支持质保兑换码（可配置质保天数）
  - 兑换码状态筛选（未使用/已使用/已过期）
  - 导出兑换码为文本文件
  - 删除未使用的兑换码

- **使用记录查询**
  - 多维度筛选（邮箱、兑换码、Team ID、日期范围）
  - 分页展示（每页20条记录）
  - 统计数据（总数、今日、本周、本月）

- **系统设置**
  - 代理配置（HTTP/SOCKS5）
  - 管理员密码修改
  - 日志级别动态调整
  - Token 预刷新频率与刷新窗口配置（降低过期导致的可用性问题）
  - **库存预警 Webhook**（基于剩余可用车位自动通知补货系统）
  - 顶部导航分区切换（点击标签仅显示当前设置板块，无需长页面滚动）
  - **公告通知管理**（支持 Markdown 公告，用户访问兑换页时弹窗展示）

- **福利车位（独立池）**
  - 福利 Team 与常规 Team 完全分池管理（导入、列表、兑换逻辑独立）
  - 福利通用兑换码为 settings 管理的虚拟码（新码生成会立即使旧码失效）
  - 福利通用码卡片支持单行紧凑展示、悬停查看完整码，并可一键复制
  - 福利通用码剩余次数按“可邀请席位”统计（`max_members - 1`），更贴合实际可拉人数量
  - 控制台保留原始 Team ID 展示，福利页使用独立“编号”便于单独核对

- **售后与风控能力**
  - 质保查询：支持按邮箱或兑换码追溯历史记录
  - 设备身份验证：支持用户一键开启 Device Code Auth
  - Team 封禁关联追踪：辅助判断是否可复用或重新分配

### 自动化与集成
- **库存预警与自动导入**
  - 当可用兑换码低于设置阈值时，自动触发 Webhook 通知
  - 支持第三方程序通过 API 自动导入新 Team 账号
  - 内置定时任务：Token 预刷新 + Team 周期状态同步（默认 7 天维度）
  - 详细对接说明见 [integration_docs.md](integration_docs.md)

### 用户功能
- **兑换流程**
  - 输入邮箱和兑换码
  - 自动验证兑换码有效性
  - 展示可用 Team 列表
  - 手动选择或自动分配 Team
  - 自动发送 Team 邀请到用户邮箱
  - 兑换首页显示常规车位与福利车位剩余数量

- **公告弹窗**
  - 管理员启用公告后，用户打开兑换页会自动弹出公告
  - 公告内容支持 Markdown（标题、列表、加粗、链接）

## 🛠️ 技术栈

- **后端框架**: FastAPI 0.109+
- **Web 服务器**: Uvicorn
- **数据库**: SQLite + SQLAlchemy 2.0 + aiosqlite
- **模板引擎**: Jinja2
- **HTTP 客户端**: curl-cffi（模拟浏览器指纹，绕过 Cloudflare 防护）
- **调度任务**: APScheduler（Token 预刷新、Team 周期同步）
- **认证**: Session-based（bcrypt 密码哈希）
- **加密**: cryptography（AES-256-GCM）
- **JWT 解析**: PyJWT
- **前端**: HTML + CSS + 原生 JavaScript

## 📋 系统要求

- Python 3.10+
- pip（Python 包管理器）
- 操作系统：Windows / Linux / macOS

## 🚀 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/loLollipop/team-manage-refresh.git
cd team-manage-refresh
```

### 2. 创建虚拟环境

```bash
# Windows
python -m venv venv
venv\Scripts\activate

# Linux/macOS
python3 -m venv venv
source venv/bin/activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 配置环境变量

复制 `.env.example` 为 `.env` 并修改配置：

```bash
cp .env.example .env
```

编辑 `.env` 文件：

```env
# 应用配置
APP_NAME=GPT Team 管理系统
APP_VERSION=0.1.0
APP_HOST=0.0.0.0
APP_PORT=8008
DEBUG=True

# 数据库配置（默认使用 SQLite）
DATABASE_URL=sqlite+aiosqlite:///team_manage.db

# 安全配置（生产环境请修改）
SECRET_KEY=your-secret-key-here-change-in-production
ADMIN_PASSWORD=admin123

# 日志配置
LOG_LEVEL=INFO

# 代理配置（可选）
PROXY_ENABLED=False
PROXY=

# JWT 配置
JWT_VERIFY_SIGNATURE=False
```

### 5. 初始化数据库

```bash
python init_db.py
```

### 6. 启动应用

```bash
# 开发模式（自动重载）
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8008

# 或者直接运行
python -m app.main
```

### 7. 访问应用

- **用户兑换页面**: http://localhost:8008/
- **管理员登录页面**: http://localhost:8008/login
- **管理员控制台**: http://localhost:8008/admin
- **福利车位管理页面**: http://localhost:8008/admin/welfare

**默认管理员账号**:
- 用户名: `admin`
- 密码: `admin123`（请在首次登录后修改）

---

## 🐳 Docker 部署 (推荐)

项目支持使用 Docker 快速部署，确保环境一致性并简化配置。

### 1. 准备工作

确保你的系统已安装：
- Docker
- Docker Compose

### 2. 快速启动

1.  克隆项目并进入目录。
2.  配置 `.env` 文件（参考上述"配置环境变量"章节）。
3.  运行 Docker Compose 命令：

```bash
# 构建并启动容器
docker compose up -d
```

### 3. 数据持久化

Docker 配置中已自动将宿主机的 `team_manage.db` 文件映射到容器内部，因此你的数据会自动保存在项目根目录下，容器删除后数据依然存在。

### 4. 常用命令

```bash
# 查看日志
docker compose logs -f

# 停止并移除容器
docker compose down

# 重新构建镜像
docker compose build --no-cache
```

## 📁 项目结构

```
team-manage/
├── app/                        # 应用主目录
│   ├── main.py                 # FastAPI 入口文件
│   ├── config.py               # 配置管理
│   ├── database.py             # 数据库连接
│   ├── models.py               # SQLAlchemy 模型
│   ├── routes/                 # 路由模块
│   │   ├── admin.py            # 管理员路由
│   │   ├── user.py             # 用户路由
│   │   ├── api.py              # API 端点
│   │   ├── auth.py             # 认证路由
│   │   └── redeem.py           # 兑换路由
│   ├── services/               # 业务逻辑服务
│   │   ├── auth.py             # 认证服务
│   │   ├── chatgpt.py          # ChatGPT API 集成
│   │   ├── encryption.py       # 加密服务
│   │   ├── redeem_flow.py      # 兑换流程服务
│   │   ├── redemption.py       # 兑换码管理服务
│   │   ├── settings.py         # 系统设置服务
│   │   └── team.py             # Team 管理服务
│   ├── utils/                  # 工具模块
│   │   ├── jwt_parser.py       # JWT Token 解析
│   │   └── token_parser.py     # Token 正则匹配
│   ├── dependencies/           # FastAPI 依赖
│   │   └── auth.py             # 认证依赖
│   ├── templates/              # Jinja2 模板
│   │   ├── base.html           # 基础布局
│   │   ├── auth/               # 认证页面
│   │   ├── admin/              # 管理员页面
│   │   └── user/               # 用户页面
│   └── static/                 # 静态文件
│       ├── css/                # 样式文件
│       └── js/                 # JavaScript 文件
├── init_db.py                  # 数据库初始化脚本
├── requirements.txt            # Python 依赖
├── Dockerfile                  # Docker 镜像构建文件
├── docker-compose.yml          # Docker 服务编排文件
├── .dockerignore               # Docker 忽略文件
├── .env.example                # 环境变量示例
├── CLAUDE.md                   # Claude Code 指南
├── 需求.md                     # 项目需求文档
├── 任务.md                     # 任务跟踪文档
├── 接口.md                     # API 接口文档
└── README.md                   # 项目说明文档
```

## 🔧 配置说明

### 数据库配置

默认使用 SQLite 数据库，数据库文件为 `team_manage.db`。如需使用其他数据库，请修改 `DATABASE_URL`。

### 代理配置

如果需要通过代理访问 ChatGPT API，可以在管理员面板的"系统设置"中配置代理：

- 支持 HTTP 代理：`http://proxy.example.com:8080`
- 支持 SOCKS5 代理：`socks5://proxy.example.com:1080`

### 安全配置

**生产环境部署前，请务必修改以下配置**：

1. `SECRET_KEY`: 用于 Session 签名，请使用随机字符串
2. `ADMIN_PASSWORD`: 管理员初始密码，首次登录后请立即修改
3. `DEBUG`: 生产环境请设置为 `False`

## 📖 使用指南

### 管理员操作流程

1. **登录管理员面板**
   - 访问 http://localhost:8008/login
   - 使用默认账号登录（admin/admin123）
   - 首次登录后建议修改密码

2. **导入 Team 账号**
   - 进入"Team 管理" → "导入 Team"
   - 单个导入支持两种模式：
     - 一键获取 Token（授权链接 → 粘贴回调 → 解析自动填充 → 导入）
     - 已有 Token 直接填写（手动模式）
   - 批量导入支持 JSON 文件一键选择导入
   - 系统会自动识别并尽量补全邮箱、Account ID 等信息

3. **生成兑换码**
   - 进入"兑换码管理" → "生成兑换码"
   - 单个生成：可自定义兑换码和有效期
   - 批量生成：设置数量和有效期
   - 生成后可复制或下载

4. **查看使用记录与售后状态**
   - 进入"使用记录"
   - 可按邮箱、兑换码、Team ID、日期范围筛选
   - 查看统计数据（总数、今日、本周、本月）
   - 需要售后时，可通过质保查询快速定位原始兑换记录

5. **系统设置与公告**
   - 进入"系统设置"可配置代理、修改密码、调整日志级别、Token 刷新与库存预警
   - 设置页支持顶部导航标签切换，仅显示当前板块，便于快速操作
   - 进入"公告通知"可编辑 Markdown 公告并控制是否启用弹窗

### 用户兑换流程

1. **访问兑换页面**
   - 访问 http://localhost:8008/

2. **输入信息**
   - 填写邮箱地址
   - 输入兑换码

3. **选择 Team**
   - 系统展示可用 Team 列表
   - 手动选择 Team 或点击"自动选择"

4. **完成兑换**
   - 系统自动发送邀请到邮箱
   - 查看兑换结果（Team 名称、到期时间）

5. **接受邀请**
   - 检查邮箱收到的 ChatGPT Team 邀请邮件
   - 点击邮件中的链接接受邀请

## 🔌 API 接口

详细的 API 接口文档请参考 [接口.md](接口.md)。

主要接口：

- `POST /auth/login` - 管理员登录
- `POST /auth/logout` - 管理员登出
- `POST /redeem/verify` - 验证兑换码
- `POST /redeem/confirm` - 确认兑换
- `GET /admin` - 管理员控制台
- `GET /admin/teams/import` - Team 导入页面
- `GET /admin/codes` - 兑换码列表
- `GET /admin/records` - 使用记录
- `POST /warranty/check` - 查询兑换码/邮箱质保状态
- `POST /warranty/enable-device-auth` - 开启设备身份验证

## 🐛 故障排除

### 数据库初始化失败

```bash
# 删除旧数据库文件
rm team_manage.db

# 重新初始化
python init_db.py
```

### 无法访问 ChatGPT API

1. 检查网络连接
2. 配置代理（如需）
3. 检查 AT Token 是否有效
4. 查看日志文件排查错误

### 导入 Team 失败

1. 确保 AT Token 格式正确
2. 检查 Token 是否过期
3. 验证 Token 是否有 Team 管理权限

## 📄 许可证

本仓库当前采用 [MIT License](./LICENSE)。

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

---

**注意**: 本系统仅用于合法的 ChatGPT Team 账号管理，请遵守 OpenAI 的服务条款。
