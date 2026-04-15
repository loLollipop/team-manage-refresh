<div align="center">
  <h1>ChatGPT Team 运营工作台</h1>
  <p>围绕 <strong>账号导入 → 兑换分配 → 质保售后 → 自动补货</strong> 打造的一体化运营后台。</p>

  <p>
    <a href="#快速开始"><img alt="Docker" src="https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white"></a>
    <a href="#功能分区"><img alt="UI" src="https://img.shields.io/badge/UI-Workspace-6366F1"></a>
    <a href="integration_docs.md"><img alt="Webhook" src="https://img.shields.io/badge/Webhook-Integration-10B981"></a>
    <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/License-MIT-success"></a>
  </p>

  <p>
    <a href="#快速开始"><strong>快速开始</strong></a> ·
    <a href="docs/manual.md"><strong>部署与操作手册</strong></a> ·
    <a href="integration_docs.md"><strong>集成文档</strong></a>
  </p>
</div>

---

## 为什么值得用

<table>
  <tr>
    <td width="33%">
      <h3>完整运营闭环</h3>
      <p>从 Team 导入、兑换分配、质保售后到库存预警与补货联动，核心流程都在同一套后台完成。</p>
    </td>
    <td width="33%">
      <h3>双池管理</h3>
      <p>常规车位与福利车位独立维护，支持福利通用码、独立统计和差异化运营策略。</p>
    </td>
    <td width="33%">
      <h3>批量工作台</h3>
      <p>支持批量导入、批量处理、批量导出和批量推送，减少运营在重复动作上的时间消耗。</p>
    </td>
  </tr>
  <tr>
    <td width="33%">
      <h3>用户前台自助化</h3>
      <p>兑换与质保查询整合在统一入口，支持自助上车、质保状态查询和重兑处理。</p>
    </td>
    <td width="33%">
      <h3>自动化维护</h3>
      <p>内置 Token 预刷新、Team 周期同步和库存预警，降低账号失效与席位不同步带来的运维成本。</p>
    </td>
    <td width="33%">
      <h3>外部系统集成</h3>
      <p>支持 Webhook 自动补货与 CliproxyAPI 推送，便于接入现有运营链路。</p>
    </td>
  </tr>
</table>

## 业务闭环

```text
导入 Team 账号
    ↓
生成兑换码 / 管理福利池
    ↓
用户自助兑换 / 查询质保
    ↓
售后追溯 / 风控排查 / 撤回邀请
    ↓
库存预警 Webhook / 外部系统自动补货
```

## 功能分区

### 用户兑换中心
- 自助上车与质保查询合并在一个入口
- 兑换页展示常规车位与福利车位剩余数量
- 支持公告弹窗，方便发布临时运营通知

### 工作台总览
- **Team 工作台**：统一查看 Team 状态、席位、成员与高频运营动作
- **兑换码工作台**：生成、筛选、导出与无效码清理集中在同一个工作区
- **使用记录工作台**：适合售后追溯、质保定位与用户问题回溯
- **系统中心**：集中管理代理、日志、库存预警、自动化任务与外部推送

### 高频操作入口
- 导入 Team、OAuth 回调解析、批量导入等高频动作统一收纳在运营弹窗里
- 适合高频执行账号导入、成员管理和后台维护任务

## 核心能力

### 运营后台
- 单个 / 批量导入 Team 账号（AT / RT / ST / Client ID）
- OAuth 授权链接生成与回调解析
- Team 成员管理、批量邀请、设备身份验证
- 双池管理：常规车位与福利车位分离运营
- 兑换码批量生成、批量修改质保、批量删除与导出
- 无效兑换码扫描与清理
- 使用记录检索、售后回溯与邀请撤回
- 公告通知、主题切换、日志级别和代理配置

### 自动化与集成
- Token 预刷新任务
- Team 周期状态同步任务
- 库存预警 Webhook
- `X-API-Key` 自动导入对接
- CliproxyAPI 推送能力

### 用户前台
- 兑换码自助激活
- 自动匹配可用 Team 并发送邀请
- 质保状态查询与重兑流程
- 剩余席位展示与公告弹窗

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/loLollipop/team-manage-refresh.git
cd team-manage-refresh
```

### 2. 准备环境变量

```bash
cp .env.example .env
```

最少建议确认这几个配置：

```env
APP_PORT=8008
SECRET_KEY=your-secret-key-here-change-in-production
ADMIN_PASSWORD=admin123
```

> 首次登录后请立即修改管理员密码。

### 3. 使用 Docker 启动

```bash
docker compose up -d
```

### 4. 访问入口

- 用户兑换页：`http://localhost:8008/`
- 管理员登录页：`http://localhost:8008/login`
- 管理后台：`http://localhost:8008/admin`
- 福利车位页：`http://localhost:8008/admin/welfare`

### 5. 常用命令

```bash
# 查看日志
docker compose logs -f

# 停止服务
docker compose down

# 重新构建
docker compose up -d --build
```

## 文档导航

- [部署与操作手册](docs/manual.md)
- [库存预警 Webhook 与自动导入对接文档](integration_docs.md)
- [环境变量示例](.env.example)
- [Docker Compose 配置](docker-compose.yml)
- [Dockerfile](Dockerfile)

## 技术栈

- FastAPI + Uvicorn
- SQLite + SQLAlchemy 2.0 + aiosqlite
- Jinja2 模板
- curl-cffi
- APScheduler
- cryptography / PyJWT
- 原生 HTML + CSS + JavaScript

## 许可证

本仓库采用 [MIT License](LICENSE)。

---

> 本项目仅用于合法的 ChatGPT Team 账号管理与运营，请遵守相关服务条款与当地法律法规。