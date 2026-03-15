"""
认证路由
处理管理员登录和登出
"""
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.auth import auth_service
from app.dependencies.auth import get_current_user

logger = logging.getLogger(__name__)

# 创建路由器
router = APIRouter(
    prefix="/auth",
    tags=["auth"]
)


# 请求模型
class LoginRequest(BaseModel):
    """登录请求"""
    password: str = Field(..., description="管理员密码", min_length=1)


class ChangePasswordRequest(BaseModel):
    """修改密码请求"""
    old_password: str = Field(..., description="旧密码", min_length=1)
    new_password: str = Field(..., description="新密码", min_length=6)


# 响应模型
class LoginResponse(BaseModel):
    """登录响应"""
    success: bool
    message: Optional[str] = None
    error: Optional[str] = None


class LogoutResponse(BaseModel):
    """登出响应"""
    success: bool
    message: str


class ChangePasswordResponse(BaseModel):
    """修改密码响应"""
    success: bool
    message: Optional[str] = None
    error: Optional[str] = None


@router.post("/login", response_model=LoginResponse)
async def login(
    request: Request,
    login_data: LoginRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    管理员登录

    Args:
        request: FastAPI Request 对象
        login_data: 登录数据
        db: 数据库会话

    Returns:
        登录结果
    """
    try:
        logger.info("管理员登录请求")

        # 验证密码
        result = await auth_service.verify_admin_login(
            login_data.password,
            db
        )

        if not result["success"]:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=result["error"]
            )

        # 设置 Session
        request.session["user"] = {
            "username": "admin",
            "is_admin": True
        }

        logger.info("管理员登录成功，Session 已创建")

        return LoginResponse(
            success=True,
            message="登录成功",
            error=None
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"登录失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"登录失败: {str(e)}"
        )


@router.post("/logout", response_model=LogoutResponse)
async def logout(request: Request):
    """
    管理员登出

    Args:
        request: FastAPI Request 对象

    Returns:
        登出结果
    """
    try:
        # 清除 Session
        request.session.clear()

        logger.info("管理员登出成功")

        return LogoutResponse(
            success=True,
            message="登出成功"
        )

    except Exception as e:
        logger.error(f"登出失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"登出失败: {str(e)}"
        )


@router.post("/change-password", response_model=ChangePasswordResponse)
async def change_password(
    request: Request,
    password_data: ChangePasswordRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    修改管理员密码

    Args:
        request: FastAPI Request 对象
        password_data: 密码数据
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        修改结果
    """
    try:
        logger.info("管理员修改密码请求")

        # 修改密码
        result = await auth_service.change_admin_password(
            password_data.old_password,
            password_data.new_password,
            db
        )

        if not result["success"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=result["error"]
            )

        # 清除 Session，要求重新登录
        request.session.clear()

        logger.info("管理员密码修改成功")

        return ChangePasswordResponse(
            success=True,
            message="密码修改成功，请重新登录",
            error=None
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"修改密码失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"修改密码失败: {str(e)}"
        )


@router.get("/status")
async def get_auth_status(request: Request):
    """
    获取认证状态

    Args:
        request: FastAPI Request 对象

    Returns:
        认证状态
    """
    user = request.session.get("user")

    return {
        "authenticated": user is not None,
        "user": user
    }
