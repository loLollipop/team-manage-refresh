"""
认证依赖
用于保护需要认证的路由
"""
import logging
from fastapi import Request, HTTPException, status
from fastapi.responses import RedirectResponse

logger = logging.getLogger(__name__)


def get_current_user(request: Request) -> dict:
    """
    获取当前登录用户
    从 Session 中获取用户信息

    Args:
        request: FastAPI Request 对象

    Returns:
        用户信息字典

    Raises:
        HTTPException: 如果未登录
    """
    user = request.session.get("user")

    if not user:
        logger.warning("未登录用户尝试访问受保护资源")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录"
        )

    return user


async def require_admin(request: Request) -> dict:
    """
    要求管理员权限
    检查用户是否已登录且具有管理员权限, 或者提供有效的 X-API-Key
    """
    # 1. 首先尝试 Session 认证
    user = request.session.get("user")
    if user and user.get("is_admin"):
        return user

    # 2. 如果 Session 不行，尝试 Header 认证 (X-API-Key)
    api_key_header = request.headers.get("X-API-Key")
    if api_key_header:
        from app.database import AsyncSessionLocal
        from app.services.settings import settings_service
        
        async with AsyncSessionLocal() as db:
            api_key = await settings_service.get_setting(db, "api_key")
            if api_key and api_key_header == api_key:
                return {"username": "api_user", "is_admin": True}

    # 3. 都没有权限
    logger.warning("认证失败: 未登录或 API Key 错误")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="未登录或 API Key 无效"
    )


def optional_user(request: Request) -> dict | None:
    """
    可选的用户信息
    如果已登录则返回用户信息，否则返回 None

    Args:
        request: FastAPI Request 对象

    Returns:
        用户信息字典或 None
    """
    return request.session.get("user")
