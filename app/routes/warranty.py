"""
质保相关路由
处理用户质保查询请求
"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import require_admin
from app.services.warranty import warranty_service

router = APIRouter(
    prefix="/warranty",
    tags=["warranty"]
)


class WarrantyCheckRequest(BaseModel):
    """质保查询请求"""
    email: Optional[EmailStr] = None
    code: Optional[str] = None


class WarrantyCheckRecord(BaseModel):
    """质保查询单条记录"""
    code: str
    has_warranty: bool
    warranty_valid: bool
    warranty_expires_at: Optional[str]
    status: str
    used_at: Optional[str]
    team_id: Optional[int]
    team_name: Optional[str]
    team_status: Optional[str]
    team_expires_at: Optional[str]
    email: Optional[str] = None
    device_code_auth_enabled: bool = False


class WarrantyCheckResponse(BaseModel):
    """质保查询响应"""
    success: bool
    has_warranty: bool
    warranty_valid: bool
    warranty_expires_at: Optional[str]
    banned_teams: list
    can_reuse: bool
    original_code: Optional[str]
    records: list[WarrantyCheckRecord] = []
    message: Optional[str]
    error: Optional[str]


@router.post("/check", response_model=WarrantyCheckResponse)
async def check_warranty(
    request: WarrantyCheckRequest,
    db_session: AsyncSession = Depends(get_db)
):
    """
    检查质保状态
    
    用户可以通过邮箱或兑换码查询质保状态
    """
    try:
        # 验证至少提供一个参数
        if not request.email and not request.code:
            raise HTTPException(
                status_code=400,
                detail="必须提供邮箱或兑换码"
            )
        
        # 调用质保服务
        result = await warranty_service.check_warranty_status(
            db_session,
            email=request.email,
            code=request.code
        )
        
        if not result["success"]:
            error_message = result.get("error", "查询失败")
            status_code = 500
            if "查询太频繁" in error_message:
                status_code = 429
            elif "必须提供" in error_message or "未找到" in error_message:
                status_code = 400
            raise HTTPException(
                status_code=status_code,
                detail=error_message
            )
        
        return WarrantyCheckResponse(
            success=True,
            has_warranty=result.get("has_warranty", False),
            warranty_valid=result.get("warranty_valid", False),
            warranty_expires_at=result.get("warranty_expires_at"),
            banned_teams=result.get("banned_teams", []),
            can_reuse=result.get("can_reuse", False),
            original_code=result.get("original_code"),
            records=result.get("records", []),
            message=result.get("message"),
            error=None
        )
        
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="查询质保状态失败，请稍后重试"
        )


class EnableDeviceAuthRequest(BaseModel):
    """开启设备身份验证请求"""
    code: str
    email: str
    team_id: int


@router.post("/enable-device-auth")
async def enable_device_auth(
    request: EnableDeviceAuthRequest,
    db_session: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    仅管理员可开启设备身份验证
    """
    from app.services.team import team_service

    try:
        res = await team_service.enable_device_code_auth(request.team_id, db_session)
        
        if not res.get("success"):
            raise HTTPException(
                status_code=500,
                detail=res.get("error", "开启失败")
            )
            
        return {"success": True, "message": "设备代码身份验证开启成功"}
        
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="开启失败，请稍后重试"
        )
