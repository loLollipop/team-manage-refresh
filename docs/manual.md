# 部署与操作手册

本文档承接主 README 中下沉的长文内容，主要覆盖：
- 本地开发部署
- Docker 部署补充说明
- 环境变量配置
- 管理员与用户操作流程
- 故障排查

## 本地开发部署

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

# Linux / macOS
python3 -m venv venv
source venv/bin/activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 配置环境变量

```bash
cp .env.example .env
```

`.env.example` 中当前主要配置如下：

```env
APP_NAME="GPT Team 管理系统"
APP_VERSION="0.1.0"
APP_HOST="0.0.0.0"
APP_PORT=8008
DEBUG=True

DATABASE_URL="sqlite+aiosqlite:///./team_manage.db"

SECRET_KEY="your-secret-key-here-change-in-production"
ADMIN_PASSWORD="admin123"

LOG_LEVEL="INFO"
DATABASE_ECHO=False

PROXY=""
PROXY_ENABLED=False

JWT_VERIFY_SIGNATURE=False
TIMEZONE=Asia/Shanghai
```

### 5. 初始化数据库

```bash
python init_db.py
```

### 6. 启动应用

```bash
# 开发模式（自动重载）
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8008

# 或直接运行
python -m app.main
```

### 7. 访问应用

- 用户兑换页面：`http://localhost:8008/`
- 管理员登录页面：`http://localhost:8008/login`
- 管理员控制台：`http://localhost:8008/admin`
- 福利车位管理页面：`http://localhost:8008/admin/welfare`

默认登录方式：
- 登录页只需要输入管理员密码
- 默认密码：`admin123`

> 首次登录后请立即修改密码。

## Docker 部署补充

项目默认推荐使用 Docker 部署。

### 快速启动

```bash
docker compose up -d
```

### 数据持久化

`docker-compose.yml` 默认会挂载：
- `./data:/app/data`
- `./.env:/app/.env`

也就是说：
- 数据库会保存在项目根目录的 `data/` 中
- 容器重建后，数据库不会丢失

### 常用命令

```bash
# 查看日志
docker compose logs -f

# 停止并移除容器
docker compose down

# 重建镜像并启动
docker compose up -d --build

# 重新构建镜像（不启动）
docker compose build --no-cache
```

## 配置说明

### 安全配置

生产环境部署前，至少确认以下配置：

1. `SECRET_KEY`
   - 用于 Session 签名
   - 请改成随机高强度字符串

2. `ADMIN_PASSWORD`
   - 管理员初始密码
   - 首次登录后请立即修改

3. `DEBUG`
   - 生产环境建议设置为 `False`

### 数据库配置

默认使用 SQLite：

```env
DATABASE_URL="sqlite+aiosqlite:///./team_manage.db"
```

Docker 环境下会被容器内路径覆盖为：

```env
DATABASE_URL=sqlite+aiosqlite:////app/data/team_manage.db
```

### 代理配置

如果需要通过代理访问 ChatGPT 相关接口，可在系统设置中配置代理。

支持格式：
- HTTP：`http://proxy.example.com:8080`
- SOCKS5：`socks5://proxy.example.com:1080`
- SOCKS5H：`socks5h://proxy.example.com:1080`

## 管理员操作流程

### 1. 登录后台
- 打开 `http://localhost:8008/login`
- 输入管理员密码登录
- 登录后进入 `/admin`

### 2. 导入 Team 账号
- 点击“导入 Team”
- 单个导入支持：
  - 一键获取 Token（授权链接 → 粘贴回调 → 自动解析）
  - 手动填写已有 Token
- 批量导入支持：
  - JSON 文件导入
  - 粘贴文本批量解析

### 3. 管理 Team
- 查看 Team 状态、剩余席位、成员列表
- 管理成员邀请、删除成员、设备身份验证
- 区分常规池与福利池运营

### 4. 生成与维护兑换码
- 批量生成兑换码
- 修改质保天数
- 导出结果
- 扫描并清理无效兑换码

### 5. 查看使用记录与售后信息
- 按邮箱、兑换码、Team ID、日期筛选
- 查询用户历史兑换记录
- 在需要时撤回邀请或排查售后问题

### 6. 系统设置
- 配置代理、日志级别
- 设置 Token 预刷新和 Team 自动同步
- 配置库存预警 Webhook
- 配置 Team 导入规则与 CliproxyAPI 推送
- 设置公告与系统主题

## 用户操作流程

### 1. 访问兑换页面
- 打开 `http://localhost:8008/`

### 2. 输入邮箱与兑换码
- 填写 ChatGPT 注册邮箱
- 输入兑换码

### 3. 完成兑换
- 系统自动验证兑换码
- 自动匹配可用 Team 并发送邀请邮件

### 4. 接受邀请
- 在邮箱中查收 ChatGPT Team 邀请
- 点击邮件中的链接加入 Team

### 5. 质保查询
- 在兑换页切换到“质保查询”
- 输入兑换码或邮箱查询质保状态
- 若符合条件，可按系统指引进行重兑

## 兼容性说明

在较新的 FastAPI / Starlette 环境中，如果模板调用仍使用旧版 `TemplateResponse` 签名，访问页面时可能报错：

```text
TypeError: unhashable type: 'dict'
```

当前仓库已经统一改成新版签名：

```python
templates.TemplateResponse(request, template_name, context)
```

如果你之前部署的是旧版本，请先更新代码后再部署。

## 故障排查

### 数据库初始化失败

本地运行时可删除项目根目录下的数据库后重新初始化：

```bash
rm team_manage.db
python init_db.py
```

如果是 Docker 部署，请删除 `data/team_manage.db` 后再重新启动容器。

Windows 可以手动删除对应数据库文件后再执行初始化。

### 无法访问 ChatGPT 接口

建议依次检查：
1. 网络连接是否正常
2. 代理配置是否正确
3. Access Token 是否有效
4. 日志中是否有明显错误提示

### 导入 Team 失败

建议依次检查：
1. Token 格式是否正确
2. Token 是否已过期
3. 该账号是否具备对应 Team 管理权限
4. Account ID / 邮箱是否匹配

### 页面打不开或样式异常

建议依次检查：
1. 容器 / 本地服务是否正常启动
2. `APP_PORT` 是否与访问端口一致
3. 浏览器缓存是否需要刷新
4. 是否使用了旧版本静态资源

## 相关文档

- [主 README](../README.md)
- [库存预警 Webhook 与自动导入对接文档](../integration_docs.md)
- [环境变量示例](../.env.example)
- [Docker Compose 配置](../docker-compose.yml)
- [Dockerfile](../Dockerfile)