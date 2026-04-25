"""
质保相关路由
处理用户质保查询请求
"""
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import require_admin
from app.services.warranty import warranty_service

router = APIRouter(
    prefix="/warranty",
    tags=["warranty"]
)


# 简易的进程内 IP 维度滑动窗口限流：仅用于 /warranty/check 和 /warranty/renewal-request
# 这种"用户匿名访问 + 可枚举对象"的场景。比 service 层基于 (email, code) 的去重更早一层，
# 防止攻击者用代理切邮箱/代理切 IP 之外，再用同一 IP 高频换邮箱探测。
# 多 worker 部署仍需在网关层（nginx / waf）补一层，这里是最低保障。
_IP_RATE_LIMITS: Dict[str, Deque[float]] = {}
_IP_RATE_LIMIT_MAX_KEYS = 50_000


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # x-forwarded-for: client, proxy1, proxy2 → 取第一个
        return forwarded.split(",", 1)[0].strip() or "unknown"
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip() or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _enforce_ip_rate_limit(
    request: Request,
    bucket: str,
    max_calls: int,
    window_seconds: int,
) -> None:
    """超过窗口内 max_calls 次直接抛 429。"""
    if max_calls <= 0 or window_seconds <= 0:
        return
    ip = _client_ip(request)
    if not ip or ip == "unknown":
        return
    key = f"{bucket}:{ip}"
    now = time.monotonic()
    window_start = now - window_seconds

    # 控制 key 总量，防止恶意制造海量假 IP 把字典撑爆
    if len(_IP_RATE_LIMITS) > _IP_RATE_LIMIT_MAX_KEYS:
        # 简单粗暴：超额时清掉最早的一半
        for stale_key in list(_IP_RATE_LIMITS.keys())[: len(_IP_RATE_LIMITS) // 2]:
            _IP_RATE_LIMITS.pop(stale_key, None)

    bucket_q = _IP_RATE_LIMITS.setdefault(key, deque())
    while bucket_q and bucket_q[0] < window_start:
        bucket_q.popleft()
    if len(bucket_q) >= max_calls:
        retry_after = max(1, int(bucket_q[0] + window_seconds - now))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"操作太频繁，请 {retry_after} 秒后再试",
        )
    bucket_q.append(now)


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
    remaining_warranty_days: Optional[int] = None
    auto_kick_enabled: bool = False
    renewal_reminder_days: Optional[int] = None
    should_show_renewal_reminder: bool = False


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
    http_request: Request,
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

        # 同一 IP 60 秒内最多 30 次（含探测、刷新、续期前置查询），
        # 阻止攻击者用同一台机器随机切邮箱嗅探用户。
        _enforce_ip_rate_limit(http_request, "warranty_check", max_calls=30, window_seconds=60)
        
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


class WarrantyRenewalRequest(BaseModel):
    """用户提交续期请求"""
    email: EmailStr
    code: str
    team_id: Optional[int] = None
    source: Optional[str] = None


class EnableDeviceAuthRequest(BaseModel):
    """开启设备身份验证请求"""
    team_id: int


@router.post("/renewal-request")
async def create_warranty_renewal_request(
    request: WarrantyRenewalRequest,
    http_request: Request,
    db_session: AsyncSession = Depends(get_db)
):
    """提交质保续期请求。"""
    try:
        # 同一 IP 60 秒内最多 5 次提交。配合 service 层的归属校验，确保攻击者
        # 即使猜中了 (email, code) 也无法在短时间内灌爆管理员待办列表。
        _enforce_ip_rate_limit(http_request, "warranty_renewal", max_calls=5, window_seconds=60)

        result = await warranty_service.create_renewal_request(
            db_session,
            email=request.email,
            code=request.code,
            team_id=request.team_id,
        )
        if not result.get("success"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=result.get("error") or "提交失败",
            )
        return result
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="提交续期请求失败，请稍后重试"
        )


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
