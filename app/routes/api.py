"""
API 路由
处理 AJAX 请求的 API 端点
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import get_current_user
from app.services.team import TeamService

logger = logging.getLogger(__name__)

# 创建路由器
router = APIRouter(
    prefix="/api",
    tags=["api"]
)

# 服务实例
team_service = TeamService()


@router.get("/teams/{team_id}/refresh")
async def refresh_team(
    team_id: int,
    force: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    刷新 Team 信息

    Args:
        team_id: Team ID
        force: 是否强制刷新 Token
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        刷新结果
    """
    try:
        logger.info(f"刷新 Team {team_id} 信息, force={force}")

        result = await team_service.sync_team_info(team_id, db, force_refresh=force)

        if not result["success"]:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result
            )

        return JSONResponse(content=result)

    except Exception as e:
        logger.error(f"刷新 Team 失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": f"刷新 Team 失败: {str(e)}"
            }
        )
