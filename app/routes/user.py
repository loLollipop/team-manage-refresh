"""
用户路由
处理用户兑换页面
"""
import logging
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db

logger = logging.getLogger(__name__)

# 创建路由器
router = APIRouter(
    tags=["user"]
)


@router.get("/", response_class=HTMLResponse)
async def redeem_page(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    用户兑换页面

    Args:
        request: FastAPI Request 对象
        db: 数据库会话

    Returns:
        用户兑换页面 HTML
    """
    try:
        from app.main import templates
        from app.services.team import TeamService
        from app.services.settings import settings_service

        team_service = TeamService()
        remaining_spots = await team_service.get_total_available_seats(db, pool_type="normal")
        welfare_remaining_spots = await team_service.get_total_available_seats(db, pool_type="welfare")
        announcement_enabled_raw = await settings_service.get_setting(db, "announcement_enabled", "false")
        announcement_enabled = str(announcement_enabled_raw).lower() in {"1", "true", "yes", "on"}
        announcement_markdown = await settings_service.get_setting(db, "announcement_markdown", "")
        ui_theme = settings_service.normalize_ui_theme(await settings_service.get_setting(db, "ui_theme", "ocean"))

        logger.info(f"用户访问兑换页面，剩余车位: {remaining_spots}")

        return templates.TemplateResponse(
            "user/redeem.html",
            {
                "request": request,
                "remaining_spots": remaining_spots,
                "announcement_enabled": announcement_enabled,
                "announcement_markdown": announcement_markdown,
                "welfare_remaining_spots": welfare_remaining_spots,
                "ui_theme": ui_theme,
            }
        )

    except Exception as e:
        logger.error(f"渲染兑换页面失败: {e}")
        return HTMLResponse(
            content=f"<h1>页面加载失败</h1><p>{str(e)}</p>",
            status_code=500
        )
