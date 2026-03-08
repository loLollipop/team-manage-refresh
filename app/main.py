"""
GPT Team 管理和兑换码自动邀请系统
FastAPI 应用入口文件
"""
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from starlette.middleware.sessions import SessionMiddleware
import logging
from pathlib import Path
from datetime import datetime
import asyncio

from contextlib import asynccontextmanager
# 导入路由
from app.routes import redeem, auth, admin, api, user, warranty
from app.config import settings
from app.database import init_db, close_db, AsyncSessionLocal
from app.services.auth import auth_service
from app.services.team import team_service
from app.services.redemption import redemption_service
from app.services.settings import settings_service
from app.services.member_lifecycle import member_lifecycle_service

# 获取项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BASE_DIR / "app"

from starlette.exceptions import HTTPException as StarletteHTTPException

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期管理
    启动时初始化数据库，关闭时释放资源
    """
    auto_refresh_task = None
    auto_team_sync_task = None
    auto_cleanup_task = None
    auto_member_reminder_task = None

    async def token_auto_refresh_loop():
        logger.info("Token 自动刷新后台任务已启动")
        while True:
            interval = max(5, int(settings.token_auto_refresh_interval_seconds or 30))
            try:
                async with AsyncSessionLocal() as session:
                    runtime_config = await settings_service.get_token_auto_refresh_config(session)
                    enabled = runtime_config["enabled"]
                    interval = max(5, int(runtime_config["interval_seconds"]))
                    settings.token_auto_refresh_enabled = enabled
                    settings.token_auto_refresh_interval_seconds = interval
                    settings.token_refresh_lead_seconds = int(runtime_config["lead_seconds"])

                    if enabled:
                        await team_service.proactive_refresh_due_tokens(session)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Token 自动刷新任务异常: {e}")
            await asyncio.sleep(interval)

    async def team_auto_sync_loop():
        logger.info("Team 信息自动同步后台任务已启动")
        while True:
            interval = 24 * 60 * 60
            try:
                async with AsyncSessionLocal() as session:
                    sync_result = await team_service.sync_all_teams(session)
                    logger.info("Team 信息自动同步完成: %s", sync_result)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Team 信息自动同步任务异常: {e}")
            await asyncio.sleep(interval)


    async def auto_cleanup_loop():
        logger.info("自动清理后台任务已启动")
        while True:
            interval = 6 * 60 * 60
            try:
                async with AsyncSessionLocal() as session:
                    team_result = await team_service.cleanup_expired_teams(session, retention_days=30)
                    code_result = await redemption_service.cleanup_old_redemption_data(session, retention_days=30)
                    logger.info(
                        "自动清理完成: teams=%s, codes=%s",
                        team_result,
                        code_result
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"自动清理任务异常: {e}")
            await asyncio.sleep(interval)

    async def member_reminder_loop():
        logger.info("成员到期提醒检查后台任务已启动")
        while True:
            interval = 24 * 60 * 60
            try:
                async with AsyncSessionLocal() as session:
                    reminder_cfg = await settings_service.get_reminder_email_config(session)
                    due_days = int(reminder_cfg.get("due_days", 3))
                    result = await member_lifecycle_service.collect_due_reminders(session, due_days=due_days)

                    logger.info("成员到期提醒检查完成: %s", result)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"成员到期提醒检查任务异常: {e}")
            await asyncio.sleep(interval)

    logger.info("系统正在启动，正在初始化数据库...")
    try:
        # 0. 确保数据库目录存在
        db_file = settings.database_url.split("///")[-1]
        Path(db_file).parent.mkdir(parents=True, exist_ok=True)

        # 1. 创建数据库表
        await init_db()

        # 2. 运行自动数据库迁移
        from app.db_migrations import run_auto_migration
        run_auto_migration()

        # 3. 初始化管理员密码（如果不存在）
        async with AsyncSessionLocal() as session:
            await auth_service.initialize_admin_password(session)

        # 4. 启动 Token 自动刷新后台任务（运行时配置可在线调整）
        auto_refresh_task = asyncio.create_task(token_auto_refresh_loop())

        # 5. 启动 Team 信息自动同步任务（每 24 小时执行一次）
        auto_team_sync_task = asyncio.create_task(team_auto_sync_loop())

        # 6. 启动过期数据自动清理任务（每 6 小时执行一次）
        auto_cleanup_task = asyncio.create_task(auto_cleanup_loop())

        # 7. 启动成员到期提醒扫描任务（每 24 小时执行一次）
        auto_member_reminder_task = asyncio.create_task(member_reminder_loop())

        logger.info("数据库初始化完成")
    except Exception as e:
        logger.error(f"数据库初始化失败: {e}")

    yield

    if auto_refresh_task:
        auto_refresh_task.cancel()
        try:
            await auto_refresh_task
        except asyncio.CancelledError:
            pass

    if auto_team_sync_task:
        auto_team_sync_task.cancel()
        try:
            await auto_team_sync_task
        except asyncio.CancelledError:
            pass

    if auto_cleanup_task:
        auto_cleanup_task.cancel()
        try:
            await auto_cleanup_task
        except asyncio.CancelledError:
            pass

    if auto_member_reminder_task:
        auto_member_reminder_task.cancel()
        try:
            await auto_member_reminder_task
        except asyncio.CancelledError:
            pass

    # 关闭连接
    await close_db()
    logger.info("系统正在关闭，已释放数据库连接")


# 创建 FastAPI 应用实例
app = FastAPI(
    title="GPT Team 管理系统",
    description="ChatGPT Team 账号管理和兑换码自动邀请系统",
    version="0.1.0",
    lifespan=lifespan
)

# 全局异常处理
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """ 处理 HTTP 异常 """
    if exc.status_code in [401, 403]:
        # 检查是否是 HTML 请求
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            return RedirectResponse(url="/login")
    
    # 默认返回 JSON 响应（FastAPI 的默认行为）
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail}
    )

# 配置 Session 中间件
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="session",
    max_age=14 * 24 * 60 * 60,  # 14 天
    same_site="lax",
    https_only=False  # 开发环境设为 False，生产环境应设为 True
)

# 配置静态文件
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

# 配置模板引擎
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

# 添加模板过滤器
def format_datetime(dt):
    """格式化日期时间"""
    if not dt:
        return "-"
    if isinstance(dt, str):
        try:
            # 兼容包含时区信息的字符串
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except:
            return dt
    
    # 统一转换为北京时间显示 (如果它是 aware datetime)
    import pytz
    from app.config import settings
    if dt.tzinfo is None:
        # 如果是 naive datetime，假设它是本地时区（CST）的时间
        pass
    else:
        # 如果是 aware datetime，转换为目标时区
        tz = pytz.timezone(settings.timezone)
        dt = dt.astimezone(tz)
        
    return dt.strftime("%Y-%m-%d %H:%M")

def escape_js(value):
    """转义字符串用于 JavaScript"""
    if not value:
        return ""
    return value.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")

templates.env.filters["format_datetime"] = format_datetime
templates.env.filters["escape_js"] = escape_js

# 配置日志
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 注册路由
app.include_router(user.router)  # 用户路由(根路径)
app.include_router(redeem.router)
app.include_router(warranty.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(api.router)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """登录页面"""
    return templates.TemplateResponse(
        "auth/login.html",
        {"request": request, "user": None}
    )


@app.get("/health")
async def health_check():
    """健康检查端点"""
    return {"status": "healthy"}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """ favicon.ico 路由 """
    return FileResponse(APP_DIR / "static" / "favicon.png")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.debug
    )
