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
DEFAULT_WARRANTY_AUTO_KICK_ENABLED = False
DEFAULT_WARRANTY_AUTO_KICK_INTERVAL_HOURS = 12
MIN_WARRANTY_AUTO_KICK_INTERVAL_HOURS = 1
MAX_WARRANTY_AUTO_KICK_INTERVAL_HOURS = 24 * 7


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


def normalize_warranty_auto_kick_interval_hours(interval_hours: int) -> int:
    return max(MIN_WARRANTY_AUTO_KICK_INTERVAL_HOURS, min(MAX_WARRANTY_AUTO_KICK_INTERVAL_HOURS, interval_hours))


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


def configure_warranty_auto_kick_job(enabled: bool, interval_hours: int) -> int:
    """配置（或重配置）质保过期自动踢人任务。"""
    normalized_interval = normalize_warranty_auto_kick_interval_hours(interval_hours)
    existing_job = scheduler.get_job("warranty_auto_kick")

    if not enabled:
        if existing_job:
            scheduler.remove_job("warranty_auto_kick")
        return normalized_interval

    trigger = IntervalTrigger(hours=normalized_interval)
    if existing_job:
        scheduler.reschedule_job("warranty_auto_kick", trigger=trigger)
    else:
        scheduler.add_job(
            scheduled_warranty_auto_kick,
            trigger=trigger,
            id="warranty_auto_kick",
            replace_existing=True,
            max_instances=1,
        )

    if not scheduler.running:
        scheduler.start()

    return normalized_interval


async def configure_warranty_auto_kick_job_from_settings() -> tuple[bool, int]:
    """从系统设置读取质保过期自动踢人配置并应用到定时任务。"""
    from app.services.settings import settings_service

    async with AsyncSessionLocal() as session:
        enabled_raw = await settings_service.get_setting(
            session,
            "warranty_auto_kick_enabled",
            str(DEFAULT_WARRANTY_AUTO_KICK_ENABLED).lower(),
        )
        interval_raw = await settings_service.get_setting(
            session,
            "warranty_auto_kick_interval_hours",
            str(DEFAULT_WARRANTY_AUTO_KICK_INTERVAL_HOURS),
        )

    enabled = str(enabled_raw).lower() in {"1", "true", "yes", "on"}
    interval_hours = normalize_warranty_auto_kick_interval_hours(
        _safe_int(interval_raw, DEFAULT_WARRANTY_AUTO_KICK_INTERVAL_HOURS)
    )
    applied_interval = configure_warranty_auto_kick_job(enabled, interval_hours)
    return enabled, applied_interval


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


async def scheduled_warranty_auto_kick():
    """定时扫描已过保的质保兑换码并自动踢人、销毁兑换码。"""
    from app.services.warranty import warranty_service

    try:
        async with AsyncSessionLocal() as session:
            stats = await warranty_service.run_warranty_auto_kick(session)
            if stats.get("success"):
                logger.info(
                    "质保自动踢人完成: scanned=%s expired=%s processed=%s destroyed=%s skipped=%s failed=%s",
                    stats["scanned"],
                    stats["expired_candidates"],
                    stats["processed"],
                    stats["destroyed"],
                    stats["skipped"],
                    stats["failed"],
                )
            else:
                logger.warning(
                    "质保自动踢人任务部分失败: scanned=%s expired=%s processed=%s destroyed=%s skipped=%s failed=%s error=%s",
                    stats.get("scanned", 0),
                    stats.get("expired_candidates", 0),
                    stats.get("processed", 0),
                    stats.get("destroyed", 0),
                    stats.get("skipped", 0),
                    stats.get("failed", 0),
                    stats.get("error"),
                )
    except Exception as e:
        logger.error(f"质保自动踢人任务执行失败: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期管理
    启动时初始化数据库，关闭时释放资源
    """
    logger.info("系统正在启动，正在初始化数据库...")
    # 默认密钥 / 密码使用预警（不强制退出，避免阻断开发环境）
    if settings.secret_key == "your-secret-key-here-change-in-production":
        logger.warning(
            "检测到 secret_key 仍为默认值，请在生产环境通过 SECRET_KEY 环境变量覆盖，"
            "否则 Session 与 Token 加密都不安全。"
        )
    if settings.admin_password == "admin123":
        logger.warning(
            "检测到 admin_password 仍为默认值 admin123，请通过 ADMIN_PASSWORD 环境变量修改，"
            "或首次登录后立即在设置页更新管理员密码。"
        )
    if not settings.session_cookie_secure:
        logger.warning(
            "session_cookie_secure=False，生产环境（HTTPS）应将其设为 True，"
            "否则 Session Cookie 可能被明文传输。"
        )
    try:
        # 0. 仅在 sqlite 驱动时才尝试创建数据库目录；非文件型数据库（如 mysql/postgres）没有路径概念。
        if settings.database_url.startswith("sqlite"):
            db_file = settings.database_url.split("///")[-1]
            if db_file:
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

        warranty_auto_kick_enabled, warranty_auto_kick_interval = await configure_warranty_auto_kick_job_from_settings()
        if warranty_auto_kick_enabled:
            logger.info(
                "定时任务已启动: 每 %s 小时检查一次质保过期自动踢人",
                warranty_auto_kick_interval,
            )
        else:
            logger.info("质保过期自动踢人任务已禁用")

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
    secret_key=settings.effective_session_secret_key,
    session_cookie="session",
    max_age=14 * 24 * 60 * 60,  # 14 天
    same_site="lax",
    https_only=settings.session_cookie_secure,
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
    """转义字符串用于嵌入到 HTML 内 <script> 块中的 JS 字面量。

    直接使用 json.dumps 以同时处理：
    - 反斜杠 / 引号 / 控制字符
    - </script> 造成的标签提前闭合（ensure_ascii=True 会把 / 之前的字符转义，
      再额外把 `<` 和 `>` 转为 unicode 转义，避免 HTML 解析器先于 JS 触发）
    - U+2028 / U+2029 等 JS 行终止符
    """
    import json
    if value is None:
        value = ""
    encoded = json.dumps(str(value), ensure_ascii=True)
    return (
        encoded
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )

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
    ui_theme = "ocean"
    try:
        from app.services.settings import settings_service, DEFAULT_UI_THEME
        async with AsyncSessionLocal() as db:
            ui_theme = settings_service.normalize_ui_theme(
                await settings_service.get_setting(db, "ui_theme", DEFAULT_UI_THEME)
            )
    except Exception:
        ui_theme = "ocean"
    return templates.TemplateResponse(
        request,
        "auth/login.html",
        {"request": request, "user": None, "ui_theme": ui_theme}
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
