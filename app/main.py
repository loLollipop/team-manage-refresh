"""
GPT Team 管理和兑换码自动邀请系统
FastAPI 应用入口文件
"""
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from starlette.middleware.sessions import SessionMiddleware
import logging
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from contextlib import asynccontextmanager
# 导入路由
from app.routes import redeem, auth, admin, api, user, warranty
from app.config import settings
from app.database import init_db, close_db, AsyncSessionLocal
from app.services.auth import auth_service
from app.services.team import team_service

# 获取项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BASE_DIR / "app"

from starlette.exceptions import HTTPException as StarletteHTTPException


# 全局调度器
scheduler = AsyncIOScheduler(timezone=settings.timezone)

DEFAULT_TOKEN_REFRESH_INTERVAL_MINUTES = 30
DEFAULT_TOKEN_REFRESH_WINDOW_HOURS = 2
MIN_TOKEN_REFRESH_INTERVAL_MINUTES = 5
MAX_TOKEN_REFRESH_INTERVAL_MINUTES = 24 * 60
MIN_TOKEN_REFRESH_WINDOW_HOURS = 1
MAX_TOKEN_REFRESH_WINDOW_HOURS = 24
DEFAULT_PERIODIC_TEAM_SYNC_ENABLED = True
DEFAULT_PERIODIC_TEAM_SYNC_INTERVAL_HOURS = 12
DEFAULT_PERIODIC_TEAM_SYNC_DAYS = 7
MIN_PERIODIC_TEAM_SYNC_INTERVAL_HOURS = 1
MAX_PERIODIC_TEAM_SYNC_INTERVAL_HOURS = 24 * 7
MIN_PERIODIC_TEAM_SYNC_DAYS = 1
MAX_PERIODIC_TEAM_SYNC_DAYS = 30


def _safe_int(value, default):
    try:
        return int(str(value).strip())
    except Exception:
        return default


def normalize_token_refresh_interval(interval_minutes: int) -> int:
    return max(MIN_TOKEN_REFRESH_INTERVAL_MINUTES, min(MAX_TOKEN_REFRESH_INTERVAL_MINUTES, interval_minutes))


def normalize_token_refresh_window(window_hours: int) -> int:
    return max(MIN_TOKEN_REFRESH_WINDOW_HOURS, min(MAX_TOKEN_REFRESH_WINDOW_HOURS, window_hours))




def normalize_periodic_team_sync_interval_hours(interval_hours: int) -> int:
    return max(MIN_PERIODIC_TEAM_SYNC_INTERVAL_HOURS, min(MAX_PERIODIC_TEAM_SYNC_INTERVAL_HOURS, interval_hours))


def normalize_periodic_team_sync_days(refresh_interval_days: int) -> int:
    return max(MIN_PERIODIC_TEAM_SYNC_DAYS, min(MAX_PERIODIC_TEAM_SYNC_DAYS, refresh_interval_days))


def configure_periodic_team_sync_job(enabled: bool, interval_hours: int) -> int:
    """配置（或重配置）Team 周期状态同步任务。"""
    normalized_interval = normalize_periodic_team_sync_interval_hours(interval_hours)
    existing_job = scheduler.get_job("periodic_team_status_sync")

    if not enabled:
        if existing_job:
            scheduler.remove_job("periodic_team_status_sync")
        return normalized_interval

    trigger = IntervalTrigger(hours=normalized_interval)
    if existing_job:
        scheduler.reschedule_job("periodic_team_status_sync", trigger=trigger)
    else:
        scheduler.add_job(
            scheduled_periodic_team_status_sync,
            trigger=trigger,
            id="periodic_team_status_sync",
            replace_existing=True
        )

    if not scheduler.running:
        scheduler.start()

    return normalized_interval


async def configure_periodic_team_sync_job_from_settings() -> tuple[bool, int, int]:
    """从系统设置读取 Team 周期同步配置并应用到定时任务。"""
    from app.services.settings import settings_service

    async with AsyncSessionLocal() as session:
        enabled_raw = await settings_service.get_setting(
            session,
            "periodic_team_sync_enabled",
            str(DEFAULT_PERIODIC_TEAM_SYNC_ENABLED).lower()
        )
        interval_raw = await settings_service.get_setting(
            session,
            "periodic_team_sync_interval_hours",
            str(DEFAULT_PERIODIC_TEAM_SYNC_INTERVAL_HOURS)
        )
        days_raw = await settings_service.get_setting(
            session,
            "periodic_team_sync_days",
            str(DEFAULT_PERIODIC_TEAM_SYNC_DAYS)
        )

    enabled = str(enabled_raw).lower() in {"1", "true", "yes", "on"}
    interval_hours = normalize_periodic_team_sync_interval_hours(
        _safe_int(interval_raw, DEFAULT_PERIODIC_TEAM_SYNC_INTERVAL_HOURS)
    )
    refresh_days = normalize_periodic_team_sync_days(
        _safe_int(days_raw, DEFAULT_PERIODIC_TEAM_SYNC_DAYS)
    )

    applied_interval = configure_periodic_team_sync_job(enabled, interval_hours)
    return enabled, applied_interval, refresh_days


def configure_proactive_refresh_job(interval_minutes: int) -> int:
    """配置（或重配置）Token 预刷新任务。"""
    normalized_interval = normalize_token_refresh_interval(interval_minutes)
    trigger = IntervalTrigger(minutes=normalized_interval)

    existing_job = scheduler.get_job("proactive_refresh_tokens")
    if existing_job:
        scheduler.reschedule_job("proactive_refresh_tokens", trigger=trigger)
    else:
        scheduler.add_job(
            scheduled_proactive_refresh,
            trigger=trigger,
            id="proactive_refresh_tokens",
            replace_existing=True
        )

    if not scheduler.running:
        scheduler.start()

    return normalized_interval


async def configure_proactive_refresh_job_from_settings() -> int:
    """从系统设置读取间隔并应用到定时任务。"""
    from app.services.settings import settings_service

    async with AsyncSessionLocal() as session:
        interval_raw = await settings_service.get_setting(
            session,
            "token_refresh_interval_minutes",
            str(DEFAULT_TOKEN_REFRESH_INTERVAL_MINUTES)
        )

    interval = _safe_int(interval_raw, DEFAULT_TOKEN_REFRESH_INTERVAL_MINUTES)
    return configure_proactive_refresh_job(interval)


async def scheduled_proactive_refresh():
    """定时执行 Team Token 预刷新（间隔可配置）。"""
    from app.services.settings import settings_service

    try:
        async with AsyncSessionLocal() as session:
            window_raw = await settings_service.get_setting(
                session,
                "token_refresh_window_hours",
                str(DEFAULT_TOKEN_REFRESH_WINDOW_HOURS)
            )
            window_hours = normalize_token_refresh_window(
                _safe_int(window_raw, DEFAULT_TOKEN_REFRESH_WINDOW_HOURS)
            )
            stats = await team_service.proactive_refresh_tokens(session, refresh_window_hours=window_hours)
            logger.info(
                "Token 预刷新任务完成: total=%s refreshed=%s skipped=%s failed=%s window=%sh",
                stats["total"], stats["refreshed"], stats["skipped"], stats["failed"], window_hours
            )
    except Exception as e:
        logger.error(f"Token 预刷新任务执行失败: {e}")


async def scheduled_periodic_team_status_sync():
    """定时按配置周期同步 Team 状态（基于导入/最近同步时间）。"""
    from app.services.settings import settings_service

    try:
        async with AsyncSessionLocal() as session:
            days_raw = await settings_service.get_setting(
                session,
                "periodic_team_sync_days",
                str(DEFAULT_PERIODIC_TEAM_SYNC_DAYS)
            )
            refresh_days = normalize_periodic_team_sync_days(
                _safe_int(days_raw, DEFAULT_PERIODIC_TEAM_SYNC_DAYS)
            )

            stats = await team_service.sync_teams_due_for_periodic_refresh(
                session,
                refresh_interval_days=refresh_days
            )
            logger.info(
                "Team 周期状态同步完成: total=%s due=%s synced=%s failed=%s skipped=%s days=%s",
                stats["total"], stats["due"], stats["synced"], stats["failed"], stats["skipped"], refresh_days
            )
    except Exception as e:
        logger.error(f"Team 周期状态同步任务执行失败: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期管理
    启动时初始化数据库，关闭时释放资源
    """
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

        # 4. 启动定时任务（间隔支持系统设置动态配置）
        interval = await configure_proactive_refresh_job_from_settings()
        logger.info(f"定时任务已启动: 每 {interval} 分钟预刷新 Team Token")

        periodic_enabled, periodic_interval, periodic_days = await configure_periodic_team_sync_job_from_settings()
        if periodic_enabled:
            logger.info(
                "定时任务已启动: 每 %s 小时检查一次 Team 状态同步（每 %s 天同步）",
                periodic_interval,
                periodic_days
            )
        else:
            logger.info("Team 周期状态同步任务已禁用")

        logger.info("数据库初始化完成")
    except Exception as e:
        logger.error(f"数据库初始化失败: {e}")
    
    yield
    
    # 关闭定时任务
    if scheduler.running:
        scheduler.shutdown(wait=False)

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
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.debug and __package__ not in {None, ""}
    )
