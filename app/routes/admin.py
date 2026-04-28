"""
管理员路由
处理管理员面板的所有页面和操作
"""
import asyncio
import json
import logging
import re
import zipfile
from io import BytesIO
from typing import Any, Optional, List, Dict, Literal
from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, Response
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field

from app.database import AsyncSessionLocal, get_db
from app.dependencies.auth import require_admin
from app.services.team import TeamService
from app.services.redemption import RedemptionService
from app.services.warranty import warranty_service
from app.services.chatgpt import chatgpt_service
from app.services.settings import (
    settings_service,
    DEFAULT_WARRANTY_EXPIRATION_MODE,
    DEFAULT_UI_THEME,
)
from app.services.cliproxyapi import cliproxyapi_service
from app.models import RedemptionCode, RedemptionRecord, RenewalRequest, Team
from app.utils.time_utils import get_now
from app.utils.proxy import mask_proxy_url, normalize_proxy_url

logger = logging.getLogger(__name__)

# 创建路由器
router = APIRouter(
    prefix="/admin",
    tags=["admin"]
)

# 服务实例
team_service = TeamService()
redemption_service = RedemptionService()


async def resolve_ui_theme(db: AsyncSession) -> str:
    """获取当前系统 UI 主题。"""
    return settings_service.normalize_ui_theme(
        await settings_service.get_setting(db, "ui_theme", DEFAULT_UI_THEME)
    )


async def get_pending_renewal_request_count(db: AsyncSession) -> int:
    """获取待处理续期请求数量。"""
    result = await db.execute(
        select(func.count(RenewalRequest.id)).where(RenewalRequest.status == "pending")
    )
    return int(result.scalar() or 0)


async def resolve_admin_profile(db: AsyncSession) -> Dict[str, str]:
    """读取管理员个人资料（昵称 + 头像 data URL）。"""
    nickname = (await settings_service.get_setting(db, "admin_nickname", "") or "").strip()
    avatar = await settings_service.get_setting(db, "admin_avatar", "") or ""
    return {
        "nickname": nickname,
        "avatar": avatar,
    }


async def build_admin_base_context(
    request: Request,
    db: AsyncSession,
    current_user: dict,
    active_page: str,
) -> Dict[str, Any]:
    """构建后台页面通用模板上下文。"""
    return {
        "request": request,
        "user": current_user,
        "active_page": active_page,
        "ui_theme": await resolve_ui_theme(db),
        "pending_renewal_request_count": await get_pending_renewal_request_count(db),
        "admin_profile": await resolve_admin_profile(db),
    }


# 请求模型
class TeamImportRequest(BaseModel):
    """Team 导入请求"""
    import_type: str = Field(..., description="导入类型: single 或 batch")
    access_token: Optional[str] = Field(None, description="AT Token (单个导入)")
    id_token: Optional[str] = Field(None, description="ID Token (单个导入)")
    refresh_token: Optional[str] = Field(None, description="Refresh Token (单个导入)")
    session_token: Optional[str] = Field(None, description="Session Token (单个导入)")
    client_id: Optional[str] = Field(None, description="Client ID (单个导入)")
    email: Optional[str] = Field(None, description="邮箱 (单个导入)")
    account_id: Optional[str] = Field(None, description="Account ID (单个导入)")
    content: Optional[str] = Field(None, description="批量导入内容")
    pool_type: str = Field("normal", description="导入池类型: normal/welfare")




class OAuthAuthorizeRequest(BaseModel):
    """生成 OAuth 授权链接请求"""
    client_id: str = Field("app_EMoamEEZ73f0CkXaXp7hrann", description="OAuth Client ID")
    redirect_uri: str = Field("http://localhost:1455/auth/callback", description="回调地址")
    scope: str = Field("openid email profile offline_access", description="OAuth scope")
    audience: Optional[str] = Field(None, description="audience（可选）")
    codex_cli_simplified_flow: bool = Field(True, description="是否启用 codex 简化流程")
    id_token_add_organizations: bool = Field(True, description="是否在 id_token 中附带组织信息")


class OAuthCallbackParseRequest(BaseModel):
    """OAuth 回调解析请求"""
    callback_text: str = Field(..., description="完整回调 URL 或回调文本")
    code_verifier: Optional[str] = Field(None, description="PKCE code_verifier")
    expected_state: Optional[str] = Field(None, description="期望的 state 值")
    client_id: Optional[str] = Field("app_EMoamEEZ73f0CkXaXp7hrann", description="兜底 client_id")
    redirect_uri: str = Field("http://localhost:1455/auth/callback", description="回调地址")

class AddMemberRequest(BaseModel):
    """单邮箱成员请求"""
    email: str = Field(..., description="成员邮箱")


class DeleteMemberRequest(BaseModel):
    """删除成员请求"""
    email: Optional[str] = Field(None, description="成员邮箱")


class AddMembersRequest(BaseModel):
    """批量添加成员请求"""
    emails: List[str] = Field(..., description="成员邮箱列表")


class CodeGenerateRequest(BaseModel):
    """兑换码生成请求"""
    type: str = Field(..., description="生成类型: single 或 batch")
    code: Optional[str] = Field(None, description="自定义兑换码 (单个生成)")
    count: Optional[int] = Field(None, description="生成数量 (批量生成)")
    expires_days: Optional[int] = Field(None, description="有效期天数")
    has_warranty: bool = Field(False, description="是否为质保兑换码")
    warranty_days: int = Field(30, description="质保天数")


class WelfareCodeGenerateRequest(BaseModel):
    """福利通用兑换码生成请求"""
    team_id: int = Field(..., description="福利 Team ID")


class TeamUpdateRequest(BaseModel):
    """Team 更新请求"""
    email: Optional[str] = Field(None, description="新邮箱")
    account_id: Optional[str] = Field(None, description="新 Account ID")
    access_token: Optional[str] = Field(None, description="新 Access Token")
    id_token: Optional[str] = Field(None, description="新 ID Token")
    refresh_token: Optional[str] = Field(None, description="新 Refresh Token")
    session_token: Optional[str] = Field(None, description="新 Session Token")
    client_id: Optional[str] = Field(None, description="新 Client ID")
    max_members: Optional[int] = Field(None, description="最大成员数")
    team_name: Optional[str] = Field(None, description="Team 名称")
    status: Optional[str] = Field(None, description="状态: active/full/expired/error/banned")


class WarrantySeatToggleRequest(BaseModel):
    """Team 质保车位开关请求"""
    enabled: bool = Field(..., description="是否开启质保车位")


class CodeUpdateRequest(BaseModel):
    """兑换码更新请求"""
    has_warranty: bool = Field(..., description="是否为质保兑换码")
    warranty_days: Optional[int] = Field(None, description="质保天数")

class BulkCodeUpdateRequest(BaseModel):
    """批量兑换码更新请求"""
    codes: List[str] = Field(..., description="兑换码列表")
    has_warranty: bool = Field(..., description="是否为质保兑换码")
    warranty_days: Optional[int] = Field(None, description="质保天数")


class BulkCodeDeleteRequest(BaseModel):
    """批量兑换码删除请求"""
    codes: List[str] = Field(..., description="待删除兑换码列表")


class InvalidCodeCleanupRequest(BaseModel):
    """无效兑换码清理请求"""
    codes: List[str] = Field(..., description="待清理的无效兑换码列表")


class BulkActionRequest(BaseModel):
    """批量操作请求"""
    ids: List[int] = Field(..., description="Team ID 列表")


class BatchRefreshRequest(BaseModel):
    """批量刷新请求"""
    ids: List[int] = Field(default_factory=list, description="Team ID 列表")
    all_in_pool: bool = Field(False, description="是否刷新当前池全部 Team")
    pool_type: Optional[Literal["normal", "welfare"]] = Field(None, description="池类型")


class BulkTransferPoolRequest(BaseModel):
    """批量转池请求"""
    ids: List[int] = Field(..., description="Team ID 列表")
    target_pool_type: Literal["normal", "welfare"] = Field(..., description="目标池类型")


@router.get("/", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    page: int = 1,
    per_page: int = 20,
    search: Optional[str] = None,
    status_filter: Optional[str] = None,
    legacy_status: Optional[str] = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    管理员面板首页
    """
    try:
        from app.main import templates
        if status_filter is None and legacy_status is not None:
            status_filter = legacy_status

        logger.info(f"管理员访问控制台, search={search}, page={page}, per_page={per_page}, status_filter={status_filter}")

        # 设置每页数量
        # per_page = 20 (Removed hardcoded value)
        
        # 获取 Team 列表 (分页)
        teams_result = await team_service.get_all_teams(db, page=page, per_page=per_page, search=search, status=status_filter, pool_type="normal")
        
        # 获取统计信息 (使用专用统计方法优化)
        team_stats = await team_service.get_stats(db, pool_type="normal")

        # 计算统计数据
        stats = {
            "total_teams": team_stats["total"],
            "available_teams": team_stats["available"],
            "live_teams": team_stats["live"],
            "banned_teams": team_stats["banned"],
            "expired_teams": team_stats["expired"],
        }

        context = await build_admin_base_context(request, db, current_user, "dashboard")
        context.update({
            "teams": teams_result.get("teams", []),
            "stats": stats,
            "search": search,
            "status_filter": status_filter,
            "pagination": {
                "current_page": teams_result.get("current_page", page),
                "total_pages": teams_result.get("total_pages", 1),
                "total": teams_result.get("total", 0),
                "per_page": per_page
            }
        })
        return templates.TemplateResponse(
            request,
            "admin/index.html",
            context,
        )
    except Exception as e:
        logger.exception("加载管理员面板失败")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="加载管理员面板失败，请稍后重试"
        )




@router.get("/welfare", response_class=HTMLResponse)
async def welfare_dashboard(
    request: Request,
    page: int = 1,
    per_page: int = 20,
    search: Optional[str] = None,
    status_filter: Optional[str] = None,
    legacy_status: Optional[str] = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """福利车位管理页"""
    try:
        from app.main import templates

        if status_filter is None and legacy_status is not None:
            status_filter = legacy_status

        teams_result = await team_service.get_all_teams(db, page=page, per_page=per_page, search=search, status=status_filter, pool_type="welfare")
        team_stats = await team_service.get_stats(db, pool_type="welfare")
        remaining_spots = await team_service.get_total_available_seats(db, pool_type="welfare")
        welfare_usage = await redemption_service.get_virtual_welfare_code_usage(db)
        welfare_code = str(welfare_usage.get("welfare_code") or "")
        welfare_used = int(welfare_usage.get("used_count") or 0)
        configured_limit = max(int(welfare_usage.get("configured_limit") or 0), 0)
        remaining_count = max(int(welfare_usage.get("remaining_count") or 0), 0)

        stats = {
            "total_teams": team_stats["total"],
            "available_teams": team_stats["available"],
            "remaining_spots": remaining_spots,
            "welfare_code": welfare_code,
            "welfare_code_limit": configured_limit,
            "welfare_code_used": welfare_used,
            "welfare_code_remaining": remaining_count,
            "welfare_code_team_id": welfare_usage.get("team_id"),
            "welfare_code_team_name": welfare_usage.get("team_name"),
            "welfare_code_team_email": welfare_usage.get("team_email"),
        }

        context = await build_admin_base_context(request, db, current_user, "welfare")
        context.update({
            "teams": teams_result.get("teams", []),
            "stats": stats,
            "search": search,
            "status_filter": status_filter,
            "pagination": {
                "current_page": teams_result.get("current_page", page),
                "total_pages": teams_result.get("total_pages", 1),
                "total": teams_result.get("total", 0),
                "per_page": per_page
            }
        })
        return templates.TemplateResponse(
            request,
            "admin/index.html",
            context,
        )
    except Exception as e:
        logger.exception("加载福利车位页面失败")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="加载福利车位页面失败，请稍后重试")


@router.post("/welfare/code/generate")
async def generate_welfare_common_code(
    payload: WelfareCodeGenerateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """为指定福利 Team 生成/更新当前唯一有效的通用兑换码。"""
    try:
        team_result = await db.execute(
            select(Team).where(Team.id == payload.team_id)
        )
        source_team = team_result.scalar_one_or_none()
        if not source_team:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"success": False, "error": "指定的福利 Team 不存在"}
            )

        if source_team.pool_type != "welfare":
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "error": "只能为福利 Team 生成通用兑换码"}
            )

        if source_team.status != "active" or source_team.current_members >= source_team.max_members:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "error": "该福利 Team 当前没有可用席位，无法生成通用兑换码"}
            )

        total_seats = max(int(source_team.max_members or 0) - int(source_team.current_members or 0), 0)
        if total_seats <= 0:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "error": "该福利 Team 当前没有可用席位，无法生成通用兑换码"}
            )

        current_welfare_code = (await settings_service.get_setting(db, "welfare_common_code", "", use_cache=False) or "").strip()
        max_attempts = 10
        code = None
        for _ in range(max_attempts):
            candidate = redemption_service._generate_random_code()
            existing_result = await db.execute(
                select(RedemptionCode).where(RedemptionCode.code == candidate)
            )
            if existing_result.scalar_one_or_none():
                continue
            existing_record_result = await db.execute(
                select(RedemptionRecord).where(RedemptionRecord.code == candidate)
            )
            if existing_record_result.scalar_one_or_none():
                continue
            if current_welfare_code and candidate == current_welfare_code:
                continue
            code = candidate
            break

        if not code:
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"success": False, "error": "生成福利通用兑换码失败，请重试"}
            )

        await db.execute(
            update(RedemptionCode)
            .where(RedemptionCode.pool_type == "welfare", RedemptionCode.reusable_by_seat == True)
            .values(status="expired")
        )
        await db.commit()

        updated = await settings_service.update_settings(db, {
            "welfare_common_code": code,
            "welfare_common_code_limit": str(total_seats),
            "welfare_common_code_used_count": "0",
            "welfare_common_code_generated_at": get_now().isoformat(),
            "welfare_common_code_team_id": str(source_team.id),
        })
        if not updated:
            await db.rollback()
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"success": False, "error": "写入福利通用兑换码配置失败，请稍后重试"}
            )
        await redemption_service.ensure_virtual_welfare_shadow_code(db, code)
        await db.commit()

        return JSONResponse(content={
            "success": True,
            "code": code,
            "limit": total_seats,
            "used": 0,
            "remaining": total_seats,
            "team_id": source_team.id,
            "team_email": source_team.email,
            "team_name": source_team.team_name,
        })
    except Exception as e:
        logger.exception("生成福利通用兑换码失败")
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"success": False, "error": "操作失败，请稍后重试"})

@router.post("/teams/{team_id}/delete")
async def delete_team(
    team_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    删除 Team

    Args:
        team_id: Team ID
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        删除结果
    """
    try:
        logger.info(f"管理员删除 Team: {team_id}")

        result = await team_service.delete_team(team_id, db)

        if not result["success"]:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result
            )

        return JSONResponse(content=result)

    except Exception as e:
        logger.exception("删除 Team 失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": "删除 Team 失败，请稍后重试"
            }
        )


@router.get("/teams/{team_id}/info")
async def get_team_info(
    team_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """获取 Team 详情 (包含解密后的 Token)"""
    try:
        result = await team_service.get_team_by_id(team_id, db)
        if not result["success"]:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=result
            )
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "操作失败，请稍后重试"}
        )


@router.post("/teams/{team_id}/update")
async def update_team(
    team_id: int,
    update_data: TeamUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """更新 Team 信息"""
    try:
        result = await team_service.update_team(
            team_id=team_id,
            db_session=db,
            email=update_data.email,
            account_id=update_data.account_id,
            access_token=update_data.access_token,
            id_token=update_data.id_token,
            refresh_token=update_data.refresh_token,
            session_token=update_data.session_token,
            client_id=update_data.client_id,
            max_members=update_data.max_members,
            team_name=update_data.team_name,
            status=update_data.status
        )
        if not result["success"]:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result
            )
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "操作失败，请稍后重试"}
        )


@router.post("/teams/{team_id}/warranty-seat")
async def toggle_team_warranty_seat(
    team_id: int,
    payload: WarrantySeatToggleRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """切换 Team 质保车位开关。"""
    try:
        result = await team_service.set_warranty_seat_enabled(
            team_id=team_id,
            enabled=payload.enabled,
            db_session=db,
        )
        if not result["success"]:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result,
            )
        return JSONResponse(content=result)
    except Exception:
        logger.exception("更新 Team 质保车位开关失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "操作失败，请稍后重试"}
        )


@router.post("/teams/import")
async def team_import(
    import_data: TeamImportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    处理 Team 导入

    Args:
        import_data: 导入数据
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        导入结果
    """
    try:
        pool_type = "welfare" if (import_data.pool_type or "normal") == "welfare" else "normal"
        logger.info(f"管理员导入 Team: {import_data.import_type}, pool={pool_type}")

        if import_data.import_type == "single":
            # 单个导入 - 允许通过 AT, RT 或 ST 导入
            if not any([import_data.access_token, import_data.refresh_token, import_data.session_token]):
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={
                        "success": False,
                        "error": "必须提供 Access Token、Refresh Token 或 Session Token 其中之一"
                    }
                )

            result = await team_service.import_team_single(
                access_token=import_data.access_token,
                db_session=db,
                email=import_data.email,
                account_id=import_data.account_id,
                id_token=import_data.id_token,
                refresh_token=import_data.refresh_token,
                session_token=import_data.session_token,
                client_id=import_data.client_id,
                pool_type=pool_type
            )

            if not result["success"]:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content=result
                )

            return JSONResponse(content=result)

        elif import_data.import_type == "batch":
            # 批量导入使用 StreamingResponse
            async def progress_generator():
                async for status_item in team_service.import_team_batch(
                    text=import_data.content,
                    db_session=db,
                    pool_type=pool_type
                ):
                    yield json.dumps(status_item, ensure_ascii=False) + "\n"

            return StreamingResponse(
                progress_generator(),
                media_type="application/x-ndjson"
            )

        elif import_data.import_type == "json":
            async def progress_generator():
                async for status_item in team_service.import_team_json(
                    json_text=import_data.content,
                    db_session=db,
                    pool_type=pool_type
                ):
                    yield json.dumps(status_item, ensure_ascii=False) + "\n"

            return StreamingResponse(
                progress_generator(),
                media_type="application/x-ndjson"
            )

        else:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "success": False,
                    "error": "无效的导入类型"
                }
            )

    except Exception as e:
        logger.exception("导入 Team 失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": "导入失败，请稍后重试"
            }
        )



@router.post("/oauth/openai/authorize")
async def create_openai_oauth_authorize_url(
    payload: OAuthAuthorizeRequest,
    current_user: dict = Depends(require_admin)
):
    """生成 OpenAI OAuth 授权链接。"""
    try:
        client_id = (payload.client_id or "").strip()
        if not client_id:
            return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"success": False, "error": "client_id 不能为空"})

        auth_data = chatgpt_service.create_oauth_authorize_url(
            client_id=client_id,
            redirect_uri=payload.redirect_uri.strip(),
            scope=payload.scope.strip() or "openid email profile offline_access",
            audience=(payload.audience.strip() if payload.audience else None),
            codex_cli_simplified_flow=payload.codex_cli_simplified_flow,
            id_token_add_organizations=payload.id_token_add_organizations,
        )

        return JSONResponse(content={"success": True, "data": {
            "authorize_url": auth_data["authorize_url"],
            "code_verifier": auth_data["code_verifier"],
            "state": auth_data["state"],
            "client_id": client_id
        }})
    except Exception as e:
        logger.exception("生成 OAuth 授权链接失败")
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"success": False, "error": "操作失败，请稍后重试"})


@router.post("/oauth/openai/parse-callback")
async def parse_openai_oauth_callback(
    payload: OAuthCallbackParseRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """解析 OAuth 回调内容并提取 token。"""
    from urllib.parse import parse_qs, urlparse

    try:
        text = (payload.callback_text or "").strip()
        if not text:
            return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"success": False, "error": "回调内容不能为空"})

        parsed = urlparse(text)
        query = parse_qs(parsed.query)
        fragment = parse_qs(parsed.fragment)

        merged: Dict[str, str] = {}
        for source in (query, fragment):
            for k, v in source.items():
                if v:
                    merged[k] = v[0]

        # 兼容非标准粘贴内容（如日志文本/JSON片段）
        if not merged:
            pairs = re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)=([^\s&]+)', text)
            for k, v in pairs:
                if k not in merged:
                    merged[k] = v

        # 兼容直接粘贴 JSON 的场景
        if "{" in text and "}" in text:
            try:
                json_candidate = json.loads(text)
                if isinstance(json_candidate, dict):
                    for key in ("access_token", "refresh_token", "id_token", "client_id", "account_id", "email", "expired", "last_refresh", "type"):
                        value = json_candidate.get(key)
                        if value and key not in merged:
                            merged[key] = str(value)
            except Exception:
                pass

        # 兜底直接提取 token/client_id
        if not merged.get("access_token"):
            m = re.search(r'(eyJ[a-zA-Z0-9_\-.]+\.[a-zA-Z0-9_\-.]+\.[a-zA-Z0-9_\-.]+)', text)
            if m:
                merged["access_token"] = m.group(1)
        if not merged.get("id_token"):
            token_matches = re.findall(r'(eyJ[a-zA-Z0-9_\-.]+\.[a-zA-Z0-9_\-.]+\.[a-zA-Z0-9_\-.]+)', text)
            if len(token_matches) >= 2:
                merged["id_token"] = token_matches[1]
        if not merged.get("refresh_token"):
            m = re.search(r'(rt[_-][A-Za-z0-9._-]+)', text)
            if m:
                merged["refresh_token"] = m.group(1)
        if not merged.get("client_id"):
            m = re.search(r'(app_[A-Za-z0-9]+)', text)
            if m:
                merged["client_id"] = m.group(1)

        if payload.expected_state and merged.get("state") and merged.get("state") != payload.expected_state:
            return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"success": False, "error": "state 不匹配，请重新生成授权链接"})

        access_token = merged.get("access_token")
        refresh_token = merged.get("refresh_token")
        id_token = merged.get("id_token")
        client_id = merged.get("client_id") or payload.client_id

        # 如果回调中只有 code，尝试自动换取 AT/RT
        code = merged.get("code")
        if code and not access_token:
            if not payload.code_verifier:
                return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={
                    "success": False,
                    "error": "回调中是 code 流程，需要 code_verifier 才能兑换 token"
                })
            if not client_id:
                return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={
                    "success": False,
                    "error": "缺少 client_id，无法兑换 token"
                })

            exchange = await chatgpt_service.exchange_oauth_code(
                code=code,
                client_id=client_id,
                redirect_uri=payload.redirect_uri.strip(),
                code_verifier=payload.code_verifier.strip(),
                db_session=db,
                identifier=f"oauth_{current_user.get('username', 'admin')}"
            )
            if not exchange.get("success"):
                return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content=exchange)

            access_token = exchange.get("access_token")
            refresh_token = exchange.get("refresh_token")
            id_token = exchange.get("id_token")
            if id_token:
                merged["id_token"] = id_token

        if not access_token and not refresh_token:
            return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={
                "success": False,
                "error": "未在回调内容中解析到 access_token/refresh_token 或可兑换的 code"
            })

        return JSONResponse(content={
            "success": True,
            "data": {
                "access_token": access_token or "",
                "refresh_token": refresh_token or "",
                "id_token": id_token or "",
                "client_id": client_id or "",
                "raw": merged
            }
        })
    except Exception as e:
        logger.exception("解析 OAuth 回调失败")
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"success": False, "error": "操作失败，请稍后重试"})


@router.get("/teams/{team_id}/members/list")
async def team_members_list(
    team_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    获取 Team 成员列表 (JSON)

    Args:
        team_id: Team ID
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        成员列表 JSON
    """
    try:
        # 获取成员列表
        result = await team_service.get_team_members(team_id, db)
        return JSONResponse(content=result)
    except Exception as e:
        logger.exception("获取成员列表失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": "获取成员列表失败，请稍后重试"
            }
        )


@router.post("/teams/{team_id}/members/add")
async def add_team_member(
    team_id: int,
    member_data: AddMembersRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    批量添加 Team 成员

    Args:
        team_id: Team ID
        member_data: 成员数据
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        添加结果
    """
    try:
        logger.info(f"管理员批量添加成员到 Team {team_id}: {member_data.emails}")

        result = await team_service.add_team_members(
            team_id=team_id,
            emails=member_data.emails,
            db_session=db
        )

        if not result.get("processed") and not result["success"]:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result
            )

        return JSONResponse(content=result)

    except Exception:
        logger.exception("添加成员失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": "添加成员失败，请稍后重试"
            }
        )


@router.post("/teams/{team_id}/members/{user_id}/delete")
async def delete_team_member(
    team_id: int,
    user_id: str,
    payload: DeleteMemberRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    删除 Team 成员

    Args:
        team_id: Team ID
        user_id: 用户 ID
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        删除结果
    """
    try:
        logger.info(f"管理员从 Team {team_id} 删除成员: {user_id}")

        result = await team_service.delete_team_member(
            team_id=team_id,
            user_id=user_id,
            db_session=db,
            email=payload.email,
        )

        if not result["success"]:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result
            )

        return JSONResponse(content=result)

    except Exception as e:
        logger.exception("删除成员失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": "删除成员失败，请稍后重试"
            }
        )


@router.post("/teams/{team_id}/invites/revoke")
async def revoke_team_invite(
    team_id: int,
    member_data: AddMemberRequest, # 使用相同的包含 email 的模型
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    撤回 Team 邀请

    Args:
        team_id: Team ID
        member_data: 成员数据 (包含 email)
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        撤回结果
    """
    try:
        logger.info(f"管理员从 Team {team_id} 撤回邀请: {member_data.email}")

        result = await team_service.revoke_team_invite(
            team_id=team_id,
            email=member_data.email,
            db_session=db
        )

        if not result["success"]:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result
            )

        return JSONResponse(content=result)

    except Exception as e:
        logger.exception("撤回邀请失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": "撤回邀请失败，请稍后重试"
            }
        )


@router.post("/teams/{team_id}/enable-device-auth")
async def enable_team_device_auth(
    team_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    开启 Team 的设备代码身份验证

    Args:
        team_id: Team ID
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        结果
    """
    try:
        logger.info(f"管理员开启 Team {team_id} 的设备身份验证")

        result = await team_service.enable_device_code_auth(
            team_id=team_id,
            db_session=db
        )

        if not result["success"]:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result
            )

        return JSONResponse(content=result)

    except Exception as e:
        logger.exception("开启设备身份验证失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": "操作失败，请稍后重试"
            }
        )


@router.get("/teams/{team_id}/export-json")
async def export_team_json(
    team_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """导出单个 Team 的 JSON 认证文件。"""
    try:
        logger.info("管理员导出 Team %s 的 JSON 认证文件", team_id)
        result = await cliproxyapi_service.get_team_auth_file_data(team_id, db)
        if not result.get("success"):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result
            )

        payload_bytes = json.dumps(
            result.get("payload") or {},
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        filename = str(result.get("filename") or f"team-{team_id}.json")
        quoted_filename = json.dumps(filename, ensure_ascii=False)
        headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}; filename={quoted_filename}"
        }
        return Response(content=payload_bytes, media_type="application/json; charset=utf-8", headers=headers)
    except Exception as e:
        logger.error("导出 Team %s 的 JSON 认证文件失败: %s", team_id, e)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": str(e)}
        )


@router.post("/teams/batch-export-json")
async def batch_export_team_json(
    action_data: BulkActionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """批量导出 Team 的 JSON 认证文件并打包为 zip。"""
    try:
        team_ids = [team_id for team_id in action_data.ids if isinstance(team_id, int)]
        if not team_ids:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "error": "请选择要导出的 Team"}
            )

        logger.info("管理员批量导出 %s 个 Team 的 JSON 认证文件", len(team_ids))

        zip_buffer = BytesIO()
        exported_count = 0
        failed_count = 0
        warning_count = 0
        results = []

        with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zip_file:
            for team_id in team_ids:
                result = await cliproxyapi_service.get_team_auth_file_data(team_id, db)
                if not result.get("success"):
                    failed_count += 1
                    results.append({
                        "team_id": team_id,
                        "email": result.get("email"),
                        "filename": None,
                        "warning": None,
                        "warnings": [],
                        "error": result.get("error"),
                    })
                    continue

                filename = str(result.get("filename") or f"team-{team_id}.json")
                payload_text = json.dumps(result.get("payload") or {}, ensure_ascii=False, indent=2)
                zip_file.writestr(filename, payload_text)

                exported_count += 1
                if result.get("warning"):
                    warning_count += 1
                results.append({
                    "team_id": team_id,
                    "email": result.get("email"),
                    "filename": filename,
                    "warning": result.get("warning"),
                    "warnings": result.get("warnings") or [],
                    "error": None,
                })

            if failed_count > 0:
                summary_payload = {
                    "success": failed_count == 0,
                    "message": f"批量导出完成：成功 {exported_count}，失败 {failed_count}",
                    "exported_count": exported_count,
                    "failed_count": failed_count,
                    "warning_count": warning_count,
                    "results": results,
                }
                zip_file.writestr("export-summary.json", json.dumps(summary_payload, ensure_ascii=False, indent=2))

        if exported_count == 0:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "success": False,
                    "error": "选中的 Team 都无法导出 JSON",
                    "failed_count": failed_count,
                    "results": results,
                }
            )

        zip_buffer.seek(0)
        archive_name = f"teams-json-export-{get_now().strftime('%Y%m%d%H%M%S')}.zip"
        quoted_archive_name = json.dumps(archive_name, ensure_ascii=False)
        headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{archive_name}; filename={quoted_archive_name}",
            "X-Exported-Count": str(exported_count),
            "X-Failed-Count": str(failed_count),
            "X-Warning-Count": str(warning_count),
        }
        return Response(content=zip_buffer.getvalue(), media_type="application/zip", headers=headers)
    except Exception as e:
        logger.error("批量导出 Team JSON 失败: %s", e)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": str(e)}
        )


@router.post("/teams/{team_id}/push-cliproxyapi")
async def push_team_to_cliproxyapi(
    team_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """将单个 Team 的 Codex 认证文件推送到 CliproxyAPI。"""
    try:
        logger.info("管理员推送 Team %s 到 CliproxyAPI", team_id)
        result = await cliproxyapi_service.push_team_auth_file(team_id, db)
        if not result.get("success"):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result
            )
        return JSONResponse(content=result)
    except Exception as e:
        logger.error("推送 Team %s 到 CliproxyAPI 失败: %s", team_id, e)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": str(e)}
        )


# ==================== 批量操作路由 ====================

@router.post("/teams/batch-push-cliproxyapi")
async def batch_push_teams_to_cliproxyapi(
    action_data: BulkActionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """批量推送 Team 的 Codex 认证文件到 CliproxyAPI。"""
    try:
        logger.info("管理员批量推送 %s 个 Team 到 CliproxyAPI", len(action_data.ids))

        uploaded_count = 0
        updated_count = 0
        skipped_count = 0
        warning_count = 0
        failed_count = 0
        results = []

        for team_id in action_data.ids:
            result = await cliproxyapi_service.push_team_auth_file(team_id, db)
            action = result.get("action")
            warning = result.get("warning")

            if result.get("success"):
                if action == "uploaded":
                    uploaded_count += 1
                elif action == "updated":
                    updated_count += 1
                elif action == "skipped":
                    skipped_count += 1
                if warning:
                    warning_count += 1

                results.append(
                    {
                        "team_id": team_id,
                        "email": result.get("email"),
                        "filename": result.get("filename"),
                        "action": action,
                        "warning": warning,
                        "warnings": result.get("warnings") or [],
                        "error": None,
                    }
                )
                continue

            failed_count += 1
            results.append(
                {
                    "team_id": team_id,
                    "email": result.get("email"),
                    "filename": result.get("filename"),
                    "action": None,
                    "warning": None,
                    "warnings": [],
                    "error": result.get("error"),
                }
            )

        message = (
            "批量推送完成: "
            f"新增 {uploaded_count}, 更新 {updated_count}, 跳过 {skipped_count}, 失败 {failed_count}"
        )
        if warning_count:
            message += f"，其中 {warning_count} 个 Team 缺少 id_token 或 refresh_token"

        return JSONResponse(content={
            "success": True,
            "message": message,
            "uploaded_count": uploaded_count,
            "updated_count": updated_count,
            "skipped_count": skipped_count,
            "warning_count": warning_count,
            "failed_count": failed_count,
            "results": results,
        })
    except Exception as e:
        logger.error("批量推送 Team 到 CliproxyAPI 失败: %s", e)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": str(e)}
        )


@router.post("/teams/batch-refresh")
async def batch_refresh_teams(
    action_data: BatchRefreshRequest,
    current_user: dict = Depends(require_admin)
):
    """批量刷新 Team 信息，并以流式方式返回进度。"""
    try:
        team_ids = [team_id for team_id in action_data.ids if isinstance(team_id, int)]

        if action_data.all_in_pool and team_ids:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "error": "请勿同时提交 Team 列表和整池检测参数"}
            )

        if not action_data.all_in_pool and action_data.pool_type:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "error": "仅整池检测时允许指定 Team 池"}
            )

        if action_data.all_in_pool:
            if not action_data.pool_type:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"success": False, "error": "请选择要检测的 Team 池"}
                )

            stmt = select(Team.id).where(Team.pool_type == action_data.pool_type).order_by(Team.created_at.desc())
            async with AsyncSessionLocal() as db_session:
                result = await db_session.execute(stmt)
                team_ids = [team_id for team_id in result.scalars().all() if isinstance(team_id, int)]

        if not team_ids:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "success": False,
                    "error": "当前池没有可检测的 Team" if action_data.all_in_pool else "请选择要刷新的 Team"
                }
            )

        logger.info(
            "管理员批量刷新 %s 个 Team%s",
            len(team_ids),
            f" (pool_type={action_data.pool_type})" if action_data.all_in_pool and action_data.pool_type else "",
        )

        async def progress_generator():
            success_count = 0
            failed_count = 0
            completed_count = 0
            total = len(team_ids)
            concurrency = min(3, total) if total > 0 else 1

            yield json.dumps({
                "type": "start",
                "total": total,
                "success_count": success_count,
                "failed_count": failed_count,
                "completed_count": completed_count,
                "concurrency": concurrency,
            }, ensure_ascii=False) + "\n"

            async def refresh_single_team(team_id: int) -> Dict[str, object]:
                item_success = False
                item_error = None
                item_message = None

                try:
                    async with AsyncSessionLocal() as db_session:
                        result = await team_service.sync_team_info(team_id, db_session, force_refresh=False)
                    item_success = bool(result.get("success"))
                    item_message = result.get("message")
                    item_error = result.get("error")
                except Exception as ex:
                    logger.error(f"批量刷新 Team {team_id} 时出错: {ex}")
                    item_error = str(ex)

                return {
                    "team_id": team_id,
                    "success": item_success,
                    "message": item_message,
                    "error": item_error,
                }

            for start_index in range(0, total, concurrency):
                team_batch = team_ids[start_index:start_index + concurrency]
                pending_tasks = {
                    asyncio.create_task(refresh_single_team(team_id))
                    for team_id in team_batch
                }

                while pending_tasks:
                    done_tasks, pending_tasks = await asyncio.wait(
                        pending_tasks,
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    for done_task in done_tasks:
                        item = await done_task
                        completed_count += 1
                        item_success = bool(item["success"])
                        if item_success:
                            success_count += 1
                        else:
                            failed_count += 1

                        yield json.dumps({
                            "type": "progress",
                            "current": completed_count,
                            "completed_count": completed_count,
                            "total": total,
                            "success_count": success_count,
                            "failed_count": failed_count,
                            "team_id": item["team_id"],
                            "concurrency": concurrency,
                            "last_result": {
                                "success": item_success,
                                "message": item["message"],
                                "error": item["error"],
                            }
                        }, ensure_ascii=False) + "\n"

            yield json.dumps({
                "type": "finish",
                "total": total,
                "success_count": success_count,
                "failed_count": failed_count,
                "completed_count": completed_count,
                "concurrency": concurrency,
                "message": f"批量刷新完成: 成功 {success_count}, 失败 {failed_count}"
            }, ensure_ascii=False) + "\n"

        return StreamingResponse(
            progress_generator(),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            }
        )
    except Exception:
        logger.exception("批量刷新 Team 失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "操作失败，请稍后重试"}
        )


@router.post("/teams/batch-delete")
async def batch_delete_teams(
    action_data: BulkActionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    批量删除 Team
    """
    try:
        logger.info(f"管理员批量删除 {len(action_data.ids)} 个 Team")
        
        success_count = 0
        failed_count = 0
        
        for team_id in action_data.ids:
            try:
                result = await team_service.delete_team(team_id, db)
                if result.get("success"):
                    success_count += 1
                else:
                    failed_count += 1
            except Exception as ex:
                logger.error(f"批量删除 Team {team_id} 时出错: {ex}")
                failed_count += 1
        
        return JSONResponse(content={
            "success": True,
            "message": f"批量删除完成: 成功 {success_count}, 失败 {failed_count}",
            "success_count": success_count,
            "failed_count": failed_count
        })
    except Exception as e:
        logger.exception("批量删除 Team 失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "操作失败，请稍后重试"}
        )


@router.post("/teams/batch-enable-device-auth")
async def batch_enable_device_auth(
    action_data: BulkActionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    批量开启设备代码身份验证
    """
    try:
        logger.info(f"管理员批量开启 {len(action_data.ids)} 个 Team 的设备验证")

        success_count = 0
        failed_count = 0

        for team_id in action_data.ids:
            try:
                result = await team_service.enable_device_code_auth(team_id, db)
                if result.get("success"):
                    success_count += 1
                else:
                    failed_count += 1
            except Exception as ex:
                logger.error(f"批量开启 Team {team_id} 设备验证时出错: {ex}")
                failed_count += 1

        return JSONResponse(content={
            "success": True,
            "message": f"批量处理完成: 成功 {success_count}, 失败 {failed_count}",
            "success_count": success_count,
            "failed_count": failed_count
        })
    except Exception as e:
        logger.exception("批量处理失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "操作失败，请稍后重试"}
        )


@router.post("/teams/batch-transfer-pool")
async def batch_transfer_team_pool(
    action_data: BulkTransferPoolRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """批量转移 Team 池类型。"""
    try:
        team_ids = [team_id for team_id in action_data.ids if isinstance(team_id, int)]
        if not team_ids:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "error": "请选择要转移的 Team"}
            )

        target_pool_type = "welfare" if action_data.target_pool_type == "welfare" else "normal"
        logger.info(
            "管理员批量转移 Team 池类型: count=%s, target=%s",
            len(team_ids),
            target_pool_type,
        )

        result = await team_service.batch_transfer_pool(
            ids=team_ids,
            target_pool_type=target_pool_type,
            db_session=db,
        )

        if not result.get("success"):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result,
            )

        return JSONResponse(content=result)
    except Exception:
        logger.exception("批量转移 Team 池类型失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "操作失败，请稍后重试"}
        )


# ==================== 兑换码管理路由 ====================

@router.get("/codes", response_class=HTMLResponse)
async def codes_list_page(
    request: Request,
    page: int = 1,
    per_page: int = 50,
    search: Optional[str] = None,
    status_filter: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    兑换码列表页面

    Args:
        request: FastAPI Request 对象
        page: 页码
        per_page: 每页数量
        search: 搜索关键词
        status_filter: 状态筛选
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        兑换码列表页面 HTML
    """
    try:
        from app.main import templates

        logger.info(f"管理员访问兑换码列表页面, search={search}, status={status_filter}, per_page={per_page}")

        # 获取兑换码 (分页)
        # per_page = 50 (Removed hardcoded value)
        codes_result = await redemption_service.get_all_codes(
            db, page=page, per_page=per_page, search=search, status=status_filter, pool_type="normal"
        )
        codes = codes_result.get("codes", [])
        total_codes = codes_result.get("total", 0)
        total_pages = codes_result.get("total_pages", 1)
        current_page = codes_result.get("current_page", 1)

        # 获取统计信息
        stats = await redemption_service.get_stats(db, pool_type="normal")
        # 兼容旧模版中的 status 统计名 (unused/used/expired)
        # 注意: get_stats 返回的 used 已经包含了 warranty_active

        # 格式化日期时间
        from datetime import datetime
        for code in codes:
            if code.get("created_at"):
                dt = datetime.fromisoformat(code["created_at"])
                code["created_at"] = dt.strftime("%Y-%m-%d %H:%M")
            if code.get("expires_at"):
                dt = datetime.fromisoformat(code["expires_at"])
                code["expires_at"] = dt.strftime("%Y-%m-%d %H:%M")
            if code.get("used_at"):
                dt = datetime.fromisoformat(code["used_at"])
                code["used_at"] = dt.strftime("%Y-%m-%d %H:%M")

        context = await build_admin_base_context(request, db, current_user, "codes")
        context.update({
            "codes": codes,
            "stats": stats,
            "search": search,
            "status_filter": status_filter,
            "pagination": {
                "current_page": current_page,
                "total_pages": total_pages,
                "total": total_codes,
                "per_page": per_page
            }
        })
        return templates.TemplateResponse(
            request,
            "admin/codes/index.html",
            context,
        )

    except Exception as e:
        logger.exception("加载兑换码列表页面失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="加载页面失败，请稍后重试"
        )




@router.post("/codes/generate")
async def generate_codes(
    generate_data: CodeGenerateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    处理兑换码生成

    Args:
        generate_data: 生成数据
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        生成结果
    """
    try:
        logger.info(f"管理员生成兑换码: {generate_data.type}")

        if generate_data.type == "single":
            # 单个生成
            result = await redemption_service.generate_code_single(
                db_session=db,
                code=generate_data.code,
                expires_days=generate_data.expires_days,
                has_warranty=generate_data.has_warranty,
                warranty_days=generate_data.warranty_days,
                pool_type="normal"
            )

            if not result["success"]:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content=result
                )

            return JSONResponse(content=result)

        elif generate_data.type == "batch":
            # 批量生成
            if not generate_data.count:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={
                        "success": False,
                        "error": "生成数量不能为空"
                    }
                )

            result = await redemption_service.generate_code_batch(
                db_session=db,
                count=generate_data.count,
                expires_days=generate_data.expires_days,
                has_warranty=generate_data.has_warranty,
                warranty_days=generate_data.warranty_days,
                pool_type="normal"
            )

            if not result["success"]:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content=result
                )

            return JSONResponse(content=result)

        else:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "success": False,
                    "error": "无效的生成类型"
                }
            )

    except Exception as e:
        logger.exception("生成兑换码失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": "生成失败，请稍后重试"
            }
        )


@router.post("/codes/{code}/delete")
async def delete_code(
    code: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    删除兑换码

    Args:
        code: 兑换码
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        删除结果
    """
    try:
        logger.info(f"管理员删除兑换码: {code}")

        result = await redemption_service.delete_code(code, db)

        if not result["success"]:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result
            )

        return JSONResponse(content=result)

    except Exception as e:
        logger.exception("删除兑换码失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": "删除失败，请稍后重试"
            }
        )


@router.get("/codes/invalid/scan")
async def scan_invalid_codes(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """扫描可安全清理的无效兑换码。"""
    try:
        result = await redemption_service.get_invalid_code_candidates(db, pool_type="normal")
        status_code = status.HTTP_200_OK if result["success"] else status.HTTP_400_BAD_REQUEST
        return JSONResponse(status_code=status_code, content=result)
    except Exception:
        logger.exception("扫描无效兑换码失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "扫描无效兑换码失败，请稍后重试"}
        )


@router.post("/codes/invalid/cleanup")
async def cleanup_invalid_codes(
    cleanup_data: InvalidCodeCleanupRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """批量清理扫描出的无效兑换码。"""
    try:
        result = await redemption_service.cleanup_invalid_codes(
            cleanup_data.codes,
            db,
            pool_type="normal"
        )
        status_code = status.HTTP_200_OK if result["success"] else status.HTTP_400_BAD_REQUEST
        return JSONResponse(status_code=status_code, content=result)
    except Exception:
        logger.exception("清理无效兑换码失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "清理无效兑换码失败，请稍后重试"}
        )


@router.get("/codes/export")
async def export_codes(
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    导出兑换码为Excel文件

    Args:
        search: 搜索关键词
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        兑换码Excel文件
    """
    try:
        from fastapi.responses import Response
        from datetime import datetime
        import xlsxwriter
        from io import BytesIO

        logger.info("管理员导出兑换码为Excel")

        # 获取所有兑换码 (导出不分页，传入大数量)
        codes_result = await redemption_service.get_all_codes(db, page=1, per_page=100000, search=search, pool_type="normal")
        all_codes = codes_result.get("codes", [])
        
        # 结果可能带统计信息，我们只取 codes

        # 创建Excel文件到内存
        output = BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        worksheet = workbook.add_worksheet('兑换码列表')

        # 定义格式
        header_format = workbook.add_format({
            'bold': True,
            'fg_color': '#4F46E5',
            'font_color': 'white',
            'align': 'center',
            'valign': 'vcenter',
            'border': 1
        })

        cell_format = workbook.add_format({
            'align': 'left',
            'valign': 'vcenter',
            'border': 1
        })

        # 设置列宽
        worksheet.set_column('A:A', 25)  # 兑换码
        worksheet.set_column('B:B', 12)  # 状态
        worksheet.set_column('C:C', 18)  # 创建时间
        worksheet.set_column('D:D', 18)  # 过期时间
        worksheet.set_column('E:E', 30)  # 使用者邮箱
        worksheet.set_column('F:F', 18)  # 使用时间
        worksheet.set_column('G:G', 12)  # 质保时长

        # 写入表头
        headers = ['兑换码', '状态', '创建时间', '过期时间', '使用者邮箱', '使用时间', '质保时长(天)']
        for col, header in enumerate(headers):
            worksheet.write(0, col, header, header_format)

        # 写入数据
        for row, code in enumerate(all_codes, start=1):
            status_text = {
                'unused': '未使用',
                'used': '已使用',
                'warranty_active': '质保中',
                'expired': '已过期'
            }.get(code['status'], code['status'])

            worksheet.write(row, 0, code['code'], cell_format)
            worksheet.write(row, 1, status_text, cell_format)
            worksheet.write(row, 2, code.get('created_at', '-'), cell_format)
            worksheet.write(row, 3, code.get('expires_at', '永久有效'), cell_format)
            worksheet.write(row, 4, code.get('used_by_email', '-'), cell_format)
            worksheet.write(row, 5, code.get('used_at', '-'), cell_format)
            worksheet.write(row, 6, code.get('warranty_days', '-') if code.get('has_warranty') else '-', cell_format)

        # 关闭workbook
        workbook.close()

        # 获取Excel数据
        excel_data = output.getvalue()
        output.close()

        # 生成文件名
        filename = f"redemption_codes_{get_now().strftime('%Y%m%d_%H%M%S')}.xlsx"

        # 返回Excel文件
        return Response(
            content=excel_data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f"attachment; filename={filename}"
            }
        )

    except Exception as e:
        logger.exception("导出兑换码失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="导出失败，请稍后重试"
        )


@router.post("/codes/{code}/update")
async def update_code(
    code: str,
    update_data: CodeUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """更新兑换码信息"""
    try:
        result = await redemption_service.update_code(
            code=code,
            db_session=db,
            has_warranty=update_data.has_warranty,
            warranty_days=update_data.warranty_days
        )
        if not result["success"]:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result
            )
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "操作失败，请稍后重试"}
        )


@router.post("/codes/bulk-update")
async def bulk_update_codes(
    update_data: BulkCodeUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """批量更新兑换码信息"""
    try:
        result = await redemption_service.bulk_update_codes(
            codes=update_data.codes,
            db_session=db,
            has_warranty=update_data.has_warranty,
            warranty_days=update_data.warranty_days
        )
        if not result["success"]:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result
            )
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "操作失败，请稍后重试"}
        )


@router.post("/codes/batch-delete")
async def batch_delete_codes(
    delete_data: BulkCodeDeleteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """批量删除兑换码。"""
    try:
        result = await redemption_service.bulk_delete_codes(delete_data.codes, db)
        status_code = status.HTTP_200_OK if result.get("success") else status.HTTP_400_BAD_REQUEST
        return JSONResponse(status_code=status_code, content=result)
    except Exception:
        logger.exception("批量删除兑换码失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "批量删除失败，请稍后重试"}
        )


@router.get("/records", response_class=HTMLResponse)
async def records_page(
    request: Request,
    email: Optional[str] = None,
    code: Optional[str] = None,
    team_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    page: Optional[str] = "1",
    per_page: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    使用记录页面

    Args:
        request: FastAPI Request 对象
        email: 邮箱筛选
        code: 兑换码筛选
        team_id: Team ID 筛选
        start_date: 开始日期
        end_date: 结束日期
        page: 页码
        per_page: 每页数量
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        使用记录页面 HTML
    """
    try:
        from app.main import templates
        from datetime import datetime, timedelta
        import math

        # 解析参数
        try:
            actual_team_id = int(team_id) if team_id and team_id.strip() else None
        except (ValueError, TypeError):
            actual_team_id = None
            
        try:
            page_int = int(page) if page and page.strip() else 1
        except (ValueError, TypeError):
            page_int = 1
            
        logger.info(f"管理员访问使用记录页面 (page={page_int}, per_page={per_page})")

        # 获取记录 (支持邮箱、兑换码、Team ID 筛选)
        records_result = await redemption_service.get_all_records(
            db, 
            email=email, 
            code=code, 
            team_id=actual_team_id
        )
        all_records = records_result.get("records", [])

        # 仅由于日期范围筛选目前还在内存中处理，如果未来记录数极大可以移至数据库
        filtered_records = []
        for record in all_records:
            # 日期范围筛选
            if start_date or end_date:
                try:
                    record_date = datetime.fromisoformat(record["redeemed_at"]).date()

                    if start_date:
                        start = datetime.strptime(start_date, "%Y-%m-%d").date()
                        if record_date < start:
                            continue

                    if end_date:
                        end = datetime.strptime(end_date, "%Y-%m-%d").date()
                        if record_date > end:
                            continue
                except:
                    pass

            filtered_records.append(record)

        # 获取Team信息并关联到记录
        teams_result = await team_service.get_all_teams(db)
        teams = teams_result.get("teams", [])
        team_map = {team["id"]: team for team in teams}

        # 为记录添加Team名称
        for record in filtered_records:
            team = team_map.get(record["team_id"])
            record["team_name"] = team["team_name"] if team else None

        # 计算统计数据
        now = get_now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=today_start.weekday())
        month_start = today_start.replace(day=1)

        stats = {
            "total": len(filtered_records),
            "today": 0,
            "this_week": 0,
            "this_month": 0
        }

        for record in filtered_records:
            try:
                record_time = datetime.fromisoformat(record["redeemed_at"])
                if record_time >= today_start:
                    stats["today"] += 1
                if record_time >= week_start:
                    stats["this_week"] += 1
                if record_time >= month_start:
                    stats["this_month"] += 1
            except:
                pass

        # 分页
        # per_page = 20 (Removed hardcoded value)
        total_records = len(filtered_records)
        total_pages = math.ceil(total_records / per_page) if total_records > 0 else 1

        # 确保页码有效
        if page_int < 1:
            page_int = 1
        if page_int > total_pages:
            page_int = total_pages

        start_idx = (page_int - 1) * per_page
        end_idx = start_idx + per_page
        paginated_records = filtered_records[start_idx:end_idx]

        # 格式化时间
        for record in paginated_records:
            try:
                dt = datetime.fromisoformat(record["redeemed_at"])
                record["redeemed_at"] = dt.strftime("%Y-%m-%d %H:%M:%S")
            except:
                pass

        context = await build_admin_base_context(request, db, current_user, "records")
        context.update({
            "records": paginated_records,
            "stats": stats,
            "filters": {
                "email": email,
                "code": code,
                "team_id": team_id,
                "start_date": start_date,
                "end_date": end_date
            },
            "pagination": {
                "current_page": page_int,
                "total_pages": total_pages,
                "total": total_records,
                "per_page": per_page
            }
        })
        return templates.TemplateResponse(
            request,
            "admin/records/index.html",
            context,
        )

    except Exception as e:
        logger.exception("获取使用记录失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="获取使用记录失败，请稍后重试"
        )


@router.post("/records/{record_id}/withdraw")
async def withdraw_record(
    record_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    撤中使用记录 (管理员功能)

    Args:
        record_id: 记录 ID
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        结果 JSON
    """
    try:
        logger.info(f"管理员请求撤回记录: {record_id}")
        result = await redemption_service.withdraw_record(record_id, db)

        if not result["success"]:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result
            )

        return JSONResponse(content=result)

    except Exception as e:
        logger.exception("撤回记录失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": "撤回失败，请稍后重试"
            }
        )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    系统设置页面

    Args:
        request: FastAPI Request 对象
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        系统设置页面 HTML
    """
    try:
        from app.main import templates
        from app.services.settings import settings_service

        logger.info("管理员访问系统设置页面")

        # 获取当前配置
        proxy_config = await settings_service.get_proxy_config(db)
        log_level = await settings_service.get_log_level(db)

        context = await build_admin_base_context(request, db, current_user, "settings")
        context.update({
            "proxy_enabled": proxy_config["enabled"],
            "proxy": proxy_config["proxy"],
            "log_level": log_level,
            "webhook_url": await settings_service.get_setting(db, "webhook_url", ""),
            "low_stock_threshold": await settings_service.get_setting(db, "low_stock_threshold", "10"),
            "api_key": await settings_service.get_setting(db, "api_key", ""),
            "token_refresh_interval_minutes": await settings_service.get_setting(db, "token_refresh_interval_minutes", "30"),
            "token_refresh_window_hours": await settings_service.get_setting(db, "token_refresh_window_hours", "2"),
            "token_refresh_client_id": await settings_service.get_setting(db, "token_refresh_client_id", ""),
            "periodic_team_sync_enabled": await settings_service.get_setting(db, "periodic_team_sync_enabled", "true"),
            "periodic_team_sync_interval_hours": await settings_service.get_setting(db, "periodic_team_sync_interval_hours", "12"),
            "periodic_team_sync_days": await settings_service.get_setting(db, "periodic_team_sync_days", "7"),
            "warranty_auto_kick_enabled": await settings_service.get_setting(db, "warranty_auto_kick_enabled", "false"),
            "warranty_auto_kick_enabled_since": await settings_service.get_setting(db, "warranty_auto_kick_enabled_since", ""),
            "warranty_auto_kick_interval_hours": await settings_service.get_setting(db, "warranty_auto_kick_interval_hours", "12"),
            "warranty_renewal_reminder_days": await settings_service.get_setting(db, "warranty_renewal_reminder_days", "7"),
            "auto_kick_usage_period_days": await settings_service.get_setting(db, "auto_kick_usage_period_days", "30"),
            "auto_kick_unauthorized_enabled": await settings_service.get_setting(db, "auto_kick_unauthorized_enabled", "false"),
            "auto_kick_unauthorized_enabled_since": await settings_service.get_setting(db, "auto_kick_unauthorized_enabled_since", ""),
            "auto_kick_admin_invited_enabled": await settings_service.get_setting(db, "auto_kick_admin_invited_enabled", "false"),
            "auto_kick_admin_invited_enabled_since": await settings_service.get_setting(db, "auto_kick_admin_invited_enabled_since", ""),
            "auto_kick_admin_invited_period_days": await settings_service.get_setting(db, "auto_kick_admin_invited_period_days", "30"),
            "default_team_max_members": await settings_service.get_setting(db, "default_team_max_members", "6"),
            "cliproxyapi_base_url": await settings_service.get_setting(db, "cliproxyapi_base_url", ""),
            "cliproxyapi_api_key": await settings_service.get_setting(db, "cliproxyapi_api_key", ""),
            "warranty_expiration_mode": await settings_service.get_warranty_expiration_mode(db),
            "ui_theme": settings_service.normalize_ui_theme(await settings_service.get_setting(db, "ui_theme", DEFAULT_UI_THEME)),
        })
        return templates.TemplateResponse(
            request,
            "admin/settings/index.html",
            context,
        )

    except Exception as e:
        logger.exception("获取系统设置失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="获取系统设置失败，请稍后重试"
        )


class ProxyConfigRequest(BaseModel):
    """代理配置请求"""
    enabled: bool = Field(..., description="是否启用代理")
    proxy: str = Field("", description="代理地址")


class LogLevelRequest(BaseModel):
    """日志级别请求"""
    level: str = Field(..., description="日志级别")


class WebhookSettingsRequest(BaseModel):
    """Webhook 设置请求"""
    webhook_url: str = Field("", description="Webhook URL")
    low_stock_threshold: int = Field(10, description="库存阈值")
    api_key: str = Field("", description="API Key")


class TokenRefreshSettingsRequest(BaseModel):
    """Token 自动刷新设置请求"""
    interval_minutes: int = Field(30, ge=5, le=1440, description="定时刷新间隔（分钟）")
    window_hours: int = Field(2, ge=1, le=24, description="过期前提前刷新窗口（小时）")
    client_id: str = Field("", description="OAuth Client ID（用于 RT 刷新）")


class TeamImportSettingsRequest(BaseModel):
    """Team 导入设置请求"""
    default_team_max_members: int = Field(6, ge=1, le=100, description="新导入 Team 的默认总席位")


class CliproxyapiSettingsRequest(BaseModel):
    """CliproxyAPI 推送配置请求"""
    base_url: str = Field("", description="CliproxyAPI 站点地址")
    api_key: str = Field("", description="CliproxyAPI 管理密钥")


class TeamAutoRefreshSettingsRequest(BaseModel):
    """Team 自动刷新设置请求"""
    enabled: bool = Field(True, description="是否启用 Team 周期状态自动刷新")
    interval_hours: int = Field(12, ge=1, le=168, description="检查间隔（小时）")
    refresh_interval_days: int = Field(7, ge=1, le=30, description="同步周期（天）")


class WarrantyAutoKickSettingsRequest(BaseModel):
    """兑换码过期自动踢人设置请求"""
    enabled: bool = Field(False, description="是否启用兑换码过期自动踢人")
    interval_hours: int = Field(12, ge=1, le=168, description="检查间隔（小时）")
    renewal_reminder_days: int = Field(7, ge=1, le=30, description="距离质保结束多少天内提醒续期")
    usage_period_days: int = Field(
        30,
        ge=1,
        le=3650,
        description="无质保兑换码的使用期限（天）；用于自动踢人判定，不影响质保码（质保码按 warranty_days 计算）",
    )
    unauthorized_enabled: bool = Field(
        False,
        description=(
            "是否启用'非授权成员清退'：仅清退该开关启用之后新加入、无兑换记录、"
            "且非后台手工邀请的成员。开启之前已经存在的成员永远豁免。"
        ),
    )
    admin_invited_enabled: bool = Field(
        False,
        description=(
            "是否启用'后台邀请成员过期踢人'：仅扫该开关启用之后新发出的后台邀请，"
            "超过 admin_invited_period_days 天未补发邀请则踢除。开启之前已经邀请的成员永远豁免。"
        ),
    )
    admin_invited_period_days: int = Field(
        30,
        ge=1,
        le=3650,
        description="后台邀请成员的使用期限（天）；超过该期限未补发邀请则踢除",
    )


class WarrantyExpirationSettingsRequest(BaseModel):
    """质保时长计算模式设置请求"""
    expiration_mode: Literal["first_use", "refresh_on_redeem"] = Field(
        DEFAULT_WARRANTY_EXPIRATION_MODE,
        description="质保时长计算模式"
    )


class UiThemeSettingsRequest(BaseModel):
    """系统配色设置请求"""
    theme: Literal["ocean", "warm"] = Field(DEFAULT_UI_THEME, description="系统配色主题")


class AdminProfileRequest(BaseModel):
    """管理员个人资料更新请求"""
    nickname: str = Field("", max_length=32, description="昵称")
    avatar: str = Field("", description="头像 data URL（image/* base64）；空字符串表示清除")


class AnnouncementUpdateRequest(BaseModel):
    """公告配置请求"""
    enabled: bool = Field(False, description="是否启用公告")
    markdown: str = Field("", description="公告 Markdown 内容")


class RenewalRequestAction(BaseModel):
    """续期请求处理请求"""
    extension_days: Optional[int] = Field(None, ge=1, le=365, description="续期天数")
    # admin_note 限长 500，避免 admin 误粘大段日志撑大数据库行；
    # 同时 service 层会在销毁兑换码时往 admin_note 追加销毁标记，留余量。
    admin_note: str = Field("", max_length=500, description="管理员备注")


@router.get("/settings/ui-theme")
async def get_ui_theme_settings(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """获取系统配色设置。"""
    theme = settings_service.normalize_ui_theme(
        await settings_service.get_setting(db, "ui_theme", DEFAULT_UI_THEME)
    )
    return JSONResponse(content={"success": True, "theme": theme})


@router.post("/settings/ui-theme")
async def update_ui_theme_settings(
    theme_data: UiThemeSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """更新系统配色设置。"""
    try:
        theme = settings_service.normalize_ui_theme(theme_data.theme)
        logger.info("管理员更新系统配色: %s", theme)

        success = await settings_service.update_setting(db, "ui_theme", theme)
        if success:
            return JSONResponse(content={"success": True, "message": "系统配色已保存", "theme": theme})

        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "保存失败"}
        )
    except Exception as e:
        logger.exception("更新系统配色失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "更新失败，请稍后重试"}
        )


# 头像 data URL 上限（约等于 1MB 二进制 + base64 30% 膨胀）
_ADMIN_AVATAR_MAX_LEN = 1_400_000


@router.get("/settings/profile")
async def get_admin_profile(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """获取管理员个人资料（昵称 + 头像）。"""
    profile = await resolve_admin_profile(db)
    return JSONResponse(content={"success": True, **profile})


@router.post("/settings/profile")
async def update_admin_profile(
    profile_data: AdminProfileRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """更新管理员个人资料。"""
    try:
        nickname = (profile_data.nickname or "").strip()
        avatar = (profile_data.avatar or "").strip()

        if avatar and not avatar.startswith("data:image/"):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "error": "头像格式无效，请上传图片"}
            )
        if avatar and len(avatar) > _ADMIN_AVATAR_MAX_LEN:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "error": "头像太大，请压缩后再上传"}
            )

        # 用 update_settings（复数）单事务写入，避免半成功 + 检查返回值
        success = await settings_service.update_settings(db, {
            "admin_nickname": nickname,
            "admin_avatar": avatar,
        })
        if not success:
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"success": False, "error": "保存失败，请稍后重试"}
            )

        logger.info(
            "管理员更新个人资料: nickname_len=%s, has_avatar=%s",
            len(nickname),
            bool(avatar),
        )

        return JSONResponse(content={
            "success": True,
            "message": "已保存",
            "nickname": nickname,
            "avatar": avatar,
        })
    except Exception:
        logger.exception("更新管理员个人资料失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "保存失败，请稍后重试"}
        )

@router.get("/announcement", response_class=HTMLResponse)
async def announcement_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """公告通知配置页面。"""
    try:
        from app.main import templates
        from app.services.settings import settings_service

        logger.info("管理员访问公告通知页面")

        enabled_raw = await settings_service.get_setting(db, "announcement_enabled", "false")
        announcement_enabled = str(enabled_raw).lower() in {"1", "true", "yes", "on"}
        announcement_markdown = await settings_service.get_setting(db, "announcement_markdown", "")

        context = await build_admin_base_context(request, db, current_user, "announcement")
        context.update({
            "announcement_enabled": announcement_enabled,
            "announcement_markdown": announcement_markdown,
        })
        return templates.TemplateResponse(
            request,
            "admin/announcement/index.html",
            context,
        )
    except Exception as e:
        logger.exception("获取公告设置失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="获取公告设置失败，请稍后重试"
        )


@router.post("/announcement")
async def update_announcement(
    payload: AnnouncementUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """保存公告配置。"""
    try:
        from app.services.settings import settings_service

        settings_payload = {
            "announcement_enabled": "true" if payload.enabled else "false",
            "announcement_markdown": payload.markdown.strip(),
        }
        success = await settings_service.update_settings(db, settings_payload)

        if success:
            return JSONResponse(content={"success": True, "message": "公告已保存"})

        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "保存失败"}
        )
    except Exception as e:
        logger.exception("保存公告设置失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "保存失败，请稍后重试"}
        )


@router.get("/renewal-requests", response_class=HTMLResponse)
async def renewal_requests_page(
    request: Request,
    status_filter: Optional[Literal["pending", "extended", "ignored"]] = "pending",
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """待处理续期任务页面。"""
    try:
        from app.main import templates

        result = await warranty_service.get_renewal_requests(db, status_filter=status_filter)
        if not result.get("success"):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=result.get("error") or "获取续期请求失败",
            )

        context = await build_admin_base_context(request, db, current_user, "renewal_requests")
        context.update({
            "renewal_requests": result.get("requests", []),
            "stats": {
                "total": result.get("total", 0),
                "pending": result.get("pending_count", 0),
                "extended": sum(1 for item in result.get("requests", []) if item.get("status") == "extended"),
                "ignored": sum(1 for item in result.get("requests", []) if item.get("status") == "ignored"),
            },
            "status_filter": status_filter,
        })
        return templates.TemplateResponse(
            request,
            "admin/renewal_requests/index.html",
            context,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("获取续期任务页面失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="获取续期任务页面失败，请稍后重试",
        )


@router.get("/renewal-requests/pending-count")
async def renewal_requests_pending_count(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    """轻量级"待处理任务"计数：供前端定时同步顶栏 badge，避免多标签页错位。"""
    try:
        count = await get_pending_renewal_request_count(db)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"success": True, "pending_count": count},
        )
    except Exception:
        logger.exception("获取待处理续期任务数量失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "获取待处理任务数量失败"},
        )


@router.get("/renewal-requests/api")
async def renewal_requests_api(
    status_filter: Optional[Literal["pending", "extended", "ignored"]] = "pending",
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    """续期请求 JSON 列表，供右上角"待处理任务"弹窗实时拉取。"""
    try:
        result = await warranty_service.get_renewal_requests(db, status_filter=status_filter)
        if not result.get("success"):
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"success": False, "error": result.get("error") or "获取续期请求失败"},
            )
        return JSONResponse(status_code=status.HTTP_200_OK, content=result)
    except Exception:
        logger.exception("获取续期请求列表失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "获取续期请求列表失败，请稍后重试"},
        )


@router.post("/renewal-requests/{request_id}/extend")
async def extend_renewal_request(
    request_id: int,
    payload: RenewalRequestAction,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """续期请求立即生效。"""
    try:
        extension_days = int(payload.extension_days or 0)
        result = await warranty_service.extend_warranty_request(
            db,
            request_id=request_id,
            extension_days=extension_days,
            admin_note=payload.admin_note,
        )
        status_code = status.HTTP_200_OK if result.get("success") else status.HTTP_400_BAD_REQUEST
        return JSONResponse(status_code=status_code, content=result)
    except Exception:
        logger.exception("处理续期请求失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "处理续期请求失败，请稍后重试"},
        )


@router.post("/renewal-requests/{request_id}/ignore")
async def ignore_renewal_request(
    request_id: int,
    payload: RenewalRequestAction,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """忽略续期请求。"""
    try:
        result = await warranty_service.ignore_renewal_request(
            db,
            request_id=request_id,
            admin_note=payload.admin_note,
        )
        status_code = status.HTTP_200_OK if result.get("success") else status.HTTP_400_BAD_REQUEST
        return JSONResponse(status_code=status_code, content=result)
    except Exception:
        logger.exception("忽略续期请求失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "忽略续期请求失败，请稍后重试"},
        )


@router.post("/settings/proxy")
async def update_proxy_config(
    proxy_data: ProxyConfigRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    更新代理配置

    Args:
        proxy_data: 代理配置数据
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        更新结果
    """
    try:
        from app.services.settings import settings_service

        masked_proxy = ""
        if proxy_data.proxy:
            try:
                masked_proxy = mask_proxy_url(proxy_data.proxy)
            except ValueError:
                masked_proxy = "<invalid-proxy>"
        logger.info(f"管理员更新代理配置: enabled={proxy_data.enabled}, proxy={masked_proxy}")

        # 验证代理地址格式
        if proxy_data.enabled:
            if not str(proxy_data.proxy or "").strip():
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={
                        "success": False,
                        "error": "启用代理时必须填写代理地址"
                    }
                )
            try:
                normalize_proxy_url(proxy_data.proxy)
            except ValueError as exc:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={
                        "success": False,
                        "error": str(exc)
                    }
                )

        # 更新配置
        success = await settings_service.update_proxy_config(
            db,
            proxy_data.enabled,
            proxy_data.proxy.strip() if proxy_data.proxy else ""
        )

        if success:
            # 清理 ChatGPT 服务的会话,确保下次请求使用新代理
            from app.services.chatgpt import chatgpt_service
            await chatgpt_service.clear_session()
            
            return JSONResponse(content={"success": True, "message": "代理配置已保存"})
        else:
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"success": False, "error": "保存失败"}
            )

    except Exception as e:
        logger.exception("更新代理配置失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "更新失败，请稍后重试"}
        )


@router.post("/settings/log-level")
async def update_log_level(
    log_data: LogLevelRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    更新日志级别

    Args:
        log_data: 日志级别数据
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        更新结果
    """
    try:
        from app.services.settings import settings_service

        logger.info(f"管理员更新日志级别: {log_data.level}")

        # 更新日志级别
        success = await settings_service.update_log_level(db, log_data.level)

        if success:
            return JSONResponse(content={"success": True, "message": "日志级别已保存"})
        else:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "error": "无效的日志级别"}
            )

    except Exception as e:
        logger.exception("更新日志级别失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "更新失败，请稍后重试"}
        )


@router.post("/settings/webhook")
async def update_webhook_settings(
    webhook_data: WebhookSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    更新 Webhook 和 API Key 设置
    """
    try:
        from app.services.settings import settings_service

        logger.info(f"管理员更新 Webhook/API 配置: url={webhook_data.webhook_url}, threshold={webhook_data.low_stock_threshold}")

        settings = {
            "webhook_url": webhook_data.webhook_url.strip(),
            "low_stock_threshold": str(webhook_data.low_stock_threshold),
            "api_key": webhook_data.api_key.strip()
        }

        success = await settings_service.update_settings(db, settings)

        if success:
            return JSONResponse(content={"success": True, "message": "配置已保存"})
        else:
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"success": False, "error": "保存失败"}
            )

    except Exception as e:
        logger.exception("更新配置失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "更新失败，请稍后重试"}
        )


@router.post("/settings/token-refresh")
async def update_token_refresh_settings(
    token_data: TokenRefreshSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """更新 Token 自动刷新设置。"""
    try:
        from app.main import configure_proactive_refresh_job
        from app.services.settings import settings_service

        logger.info(
            "管理员更新 Token 自动刷新配置: interval=%s, window=%s",
            token_data.interval_minutes,
            token_data.window_hours,
        )

        settings = {
            "token_refresh_interval_minutes": str(token_data.interval_minutes),
            "token_refresh_window_hours": str(token_data.window_hours),
            "token_refresh_client_id": token_data.client_id.strip(),
        }

        success = await settings_service.update_settings(db, settings)
        if not success:
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"success": False, "error": "保存失败"}
            )

        interval = configure_proactive_refresh_job(token_data.interval_minutes)
        return JSONResponse(
            content={
                "success": True,
                "message": f"Token 自动刷新配置已保存（当前间隔: {interval} 分钟）",
                "interval": interval
            }
        )

    except Exception as e:
        logger.exception("更新 Token 自动刷新设置失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "更新失败，请稍后重试"}
        )


@router.post("/settings/team-auto-refresh")
async def update_team_auto_refresh_settings(
    team_refresh_data: TeamAutoRefreshSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """更新 Team 周期状态自动刷新设置。"""
    try:
        from app.main import configure_periodic_team_sync_job
        from app.services.settings import settings_service

        logger.info(
            "管理员更新 Team 自动刷新配置: enabled=%s, interval_hours=%s, days=%s",
            team_refresh_data.enabled,
            team_refresh_data.interval_hours,
            team_refresh_data.refresh_interval_days,
        )

        settings_payload = {
            "periodic_team_sync_enabled": str(team_refresh_data.enabled).lower(),
            "periodic_team_sync_interval_hours": str(team_refresh_data.interval_hours),
            "periodic_team_sync_days": str(team_refresh_data.refresh_interval_days),
        }

        success = await settings_service.update_settings(db, settings_payload)
        if not success:
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"success": False, "error": "保存失败"}
            )

        applied_interval = configure_periodic_team_sync_job(
            team_refresh_data.enabled,
            team_refresh_data.interval_hours,
        )

        if team_refresh_data.enabled:
            message = (
                "Team 自动刷新配置已保存（每 "
                f"{applied_interval} 小时检查一次，超过 {team_refresh_data.refresh_interval_days} 天未同步则执行刷新）"
            )
        else:
            message = "Team 自动刷新已关闭"

        return JSONResponse(
            content={
                "success": True,
                "message": message,
                "enabled": team_refresh_data.enabled,
                "interval_hours": applied_interval,
                "refresh_interval_days": team_refresh_data.refresh_interval_days,
            }
        )
    except Exception as e:
        logger.exception("更新 Team 自动刷新设置失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "更新失败，请稍后重试"}
        )


@router.post("/settings/warranty")
async def update_warranty_settings(
    warranty_data: WarrantyExpirationSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """更新质保时长计算模式。"""
    try:
        expiration_mode = settings_service.normalize_warranty_expiration_mode(
            warranty_data.expiration_mode
        )
        logger.info("管理员更新质保计算模式: %s", expiration_mode)

        success = await settings_service.update_setting(
            db,
            "warranty_expiration_mode",
            expiration_mode,
        )
        if not success:
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"success": False, "error": "保存失败"}
            )

        message = (
            "质保设置已保存：按首次使用时间计算质保期"
            if expiration_mode == DEFAULT_WARRANTY_EXPIRATION_MODE
            else "质保设置已保存：质保重兑成功后刷新完整质保期"
        )
        return JSONResponse(
            content={
                "success": True,
                "message": message,
                "expiration_mode": expiration_mode,
            }
        )
    except Exception as e:
        logger.exception("更新质保设置失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "更新失败，请稍后重试"}
        )


@router.post("/settings/warranty-auto-kick")
async def update_warranty_auto_kick_settings(
    auto_kick_data: WarrantyAutoKickSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """更新兑换码过期自动踢人设置。"""
    try:
        from app.main import configure_warranty_auto_kick_job
        from app.utils.time_utils import get_now

        logger.info(
            "管理员更新自动踢人配置: enabled=%s, interval_hours=%s, reminder_days=%s, usage_period_days=%s, unauthorized_enabled=%s, admin_invited_enabled=%s, admin_invited_period_days=%s",
            auto_kick_data.enabled,
            auto_kick_data.interval_hours,
            auto_kick_data.renewal_reminder_days,
            auto_kick_data.usage_period_days,
            auto_kick_data.unauthorized_enabled,
            auto_kick_data.admin_invited_enabled,
            auto_kick_data.admin_invited_period_days,
        )

        # 处理"兑换码过期自动踢人"主开关：仅在 false→true 翻转时记录启用时间戳。
        # 启用时间戳用于豁免"开关启用之前就已经使用的兑换码"，避免对老用户突然生效。
        prev_main_raw = await settings_service.get_setting(
            db, "warranty_auto_kick_enabled", "false"
        )
        prev_main = str(prev_main_raw).strip().lower() in ("1", "true", "yes", "on")
        new_main = bool(auto_kick_data.enabled)

        # 处理"非授权成员清退"开关：仅在 false→true 翻转时记录启用时间戳。
        prev_unauth_raw = await settings_service.get_setting(
            db, "auto_kick_unauthorized_enabled", "false"
        )
        prev_unauth = str(prev_unauth_raw).strip().lower() in ("1", "true", "yes", "on")
        new_unauth = bool(auto_kick_data.unauthorized_enabled)

        # 处理"后台邀请过期踢人"开关：同样仅在 false→true 翻转时记录启用时间戳。
        prev_admin_inv_raw = await settings_service.get_setting(
            db, "auto_kick_admin_invited_enabled", "false"
        )
        prev_admin_inv = str(prev_admin_inv_raw).strip().lower() in ("1", "true", "yes", "on")
        new_admin_inv = bool(auto_kick_data.admin_invited_enabled)

        settings_to_save = {
            "warranty_auto_kick_enabled": str(auto_kick_data.enabled).lower(),
            "warranty_auto_kick_interval_hours": str(auto_kick_data.interval_hours),
            "warranty_renewal_reminder_days": str(auto_kick_data.renewal_reminder_days),
            "auto_kick_usage_period_days": str(auto_kick_data.usage_period_days),
            "auto_kick_unauthorized_enabled": str(new_unauth).lower(),
            "auto_kick_admin_invited_enabled": str(new_admin_inv).lower(),
            "auto_kick_admin_invited_period_days": str(auto_kick_data.admin_invited_period_days),
        }

        # 关→开：写入新的启用时间戳；其它情形（保持开 / 保持关 / 开→关）不动 since。
        if new_main and not prev_main:
            settings_to_save["warranty_auto_kick_enabled_since"] = get_now().isoformat()
        if new_unauth and not prev_unauth:
            settings_to_save["auto_kick_unauthorized_enabled_since"] = get_now().isoformat()
        if new_admin_inv and not prev_admin_inv:
            settings_to_save["auto_kick_admin_invited_enabled_since"] = get_now().isoformat()

        success = await settings_service.update_settings(db, settings_to_save)
        if not success:
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"success": False, "error": "保存失败"}
            )

        applied_interval = configure_warranty_auto_kick_job(
            auto_kick_data.enabled,
            auto_kick_data.interval_hours,
        )

        message_parts = []
        if auto_kick_data.enabled:
            if new_main and not prev_main:
                message_parts.append(
                    f"自动踢人已启用（每 {applied_interval} 小时检查一次）；开关启用前已使用的兑换码永久豁免"
                )
            else:
                message_parts.append(f"自动踢人配置已保存（每 {applied_interval} 小时检查一次）")
        else:
            message_parts.append("过期自动踢人已关闭")
        if new_unauth and not prev_unauth:
            message_parts.append("非授权成员清退已启用，仅清退此后新加入的非授权成员")
        elif not new_unauth and prev_unauth:
            message_parts.append("非授权成员清退已关闭")
        if new_admin_inv and not prev_admin_inv:
            message_parts.append(
                f"后台邀请过期踢人已启用，仅扫此后新发出的邀请，期限 {auto_kick_data.admin_invited_period_days} 天"
            )
        elif not new_admin_inv and prev_admin_inv:
            message_parts.append("后台邀请过期踢人已关闭")
        message = "；".join(message_parts)

        main_since_value = settings_to_save.get("warranty_auto_kick_enabled_since")
        if main_since_value is None:
            main_since_value = await settings_service.get_setting(
                db, "warranty_auto_kick_enabled_since", ""
            )
        enabled_since_value = settings_to_save.get("auto_kick_unauthorized_enabled_since")
        if enabled_since_value is None:
            enabled_since_value = await settings_service.get_setting(
                db, "auto_kick_unauthorized_enabled_since", ""
            )
        admin_inv_since_value = settings_to_save.get("auto_kick_admin_invited_enabled_since")
        if admin_inv_since_value is None:
            admin_inv_since_value = await settings_service.get_setting(
                db, "auto_kick_admin_invited_enabled_since", ""
            )

        return JSONResponse(
            content={
                "success": True,
                "message": message,
                "enabled": auto_kick_data.enabled,
                "enabled_since": main_since_value or None,
                "interval_hours": applied_interval,
                "renewal_reminder_days": auto_kick_data.renewal_reminder_days,
                "usage_period_days": auto_kick_data.usage_period_days,
                "unauthorized_enabled": new_unauth,
                "unauthorized_enabled_since": enabled_since_value or None,
                "admin_invited_enabled": new_admin_inv,
                "admin_invited_enabled_since": admin_inv_since_value or None,
                "admin_invited_period_days": auto_kick_data.admin_invited_period_days,
            }
        )
    except Exception:
        logger.exception("更新质保过期自动踢人设置失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "更新失败，请稍后重试"}
        )


@router.post("/settings/team-import")
async def update_team_import_settings(
    team_import_data: TeamImportSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """更新 Team 导入默认配置。"""
    try:
        logger.info(
            "管理员更新 Team 导入配置: default_team_max_members=%s",
            team_import_data.default_team_max_members,
        )

        success = await settings_service.update_setting(
            db,
            "default_team_max_members",
            str(team_import_data.default_team_max_members),
        )

        if success:
            return JSONResponse(content={"success": True, "message": "Team 导入配置已保存"})

        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "保存失败"}
        )

    except Exception as e:
        logger.exception("更新 Team 导入设置失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "更新失败，请稍后重试"}
        )


@router.post("/settings/cliproxyapi")
async def update_cliproxyapi_settings(
    cliproxyapi_data: CliproxyapiSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """更新 CliproxyAPI 推送配置。"""
    try:
        base_url = cliproxyapi_service.normalize_base_url(cliproxyapi_data.base_url)
        api_key = cliproxyapi_data.api_key.strip()

        if not base_url:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "error": "CliproxyAPI 地址不能为空"}
            )

        if not api_key:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "error": "CliproxyAPI 管理密钥不能为空"}
            )

        if not cliproxyapi_service.is_valid_base_url(base_url):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "error": "CliproxyAPI 地址格式错误，仅支持 http/https"}
            )

        success = await settings_service.update_settings(
            db,
            {
                "cliproxyapi_base_url": base_url,
                "cliproxyapi_api_key": api_key,
            }
        )

        if success:
            return JSONResponse(
                content={
                    "success": True,
                    "message": "CliproxyAPI 配置已保存",
                    "base_url": base_url,
                }
            )

        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "保存失败"}
        )

    except Exception as e:
        logger.error("更新 CliproxyAPI 配置失败: %s", e)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": f"更新失败: {str(e)}"}
        )
