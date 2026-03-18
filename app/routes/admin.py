"""
管理员路由
处理管理员面板的所有页面和操作
"""
import logging
import re
from typing import Optional, List, Dict, Literal
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
import json
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field

from app.database import get_db
from app.dependencies.auth import require_admin
from app.services.team import TeamService
from app.services.redemption import RedemptionService
from app.services.chatgpt import chatgpt_service
from app.services.settings import (
    settings_service,
    DEFAULT_WARRANTY_EXPIRATION_MODE,
    DEFAULT_UI_THEME,
)
from app.models import RedemptionCode, Team
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)

# 创建路由器
router = APIRouter(
    prefix="/admin",
    tags=["admin"]
)

import json

# 服务实例
team_service = TeamService()
redemption_service = RedemptionService()


async def resolve_ui_theme(db: AsyncSession) -> str:
    """获取当前系统 UI 主题。"""
    return settings_service.normalize_ui_theme(
        await settings_service.get_setting(db, "ui_theme", DEFAULT_UI_THEME)
    )



# 请求模型
class TeamImportRequest(BaseModel):
    """Team 导入请求"""
    import_type: str = Field(..., description="导入类型: single 或 batch")
    access_token: Optional[str] = Field(None, description="AT Token (单个导入)")
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
    """添加成员请求"""
    email: str = Field(..., description="成员邮箱")


class CodeGenerateRequest(BaseModel):
    """兑换码生成请求"""
    type: str = Field(..., description="生成类型: single 或 batch")
    code: Optional[str] = Field(None, description="自定义兑换码 (单个生成)")
    count: Optional[int] = Field(None, description="生成数量 (批量生成)")
    expires_days: Optional[int] = Field(None, description="有效期天数")
    has_warranty: bool = Field(False, description="是否为质保兑换码")
    warranty_days: int = Field(30, description="质保天数")


class TeamUpdateRequest(BaseModel):
    """Team 更新请求"""
    email: Optional[str] = Field(None, description="新邮箱")
    account_id: Optional[str] = Field(None, description="新 Account ID")
    access_token: Optional[str] = Field(None, description="新 Access Token")
    refresh_token: Optional[str] = Field(None, description="新 Refresh Token")
    session_token: Optional[str] = Field(None, description="新 Session Token")
    client_id: Optional[str] = Field(None, description="新 Client ID")
    max_members: Optional[int] = Field(None, description="最大成员数")
    team_name: Optional[str] = Field(None, description="Team 名称")
    status: Optional[str] = Field(None, description="状态: active/full/expired/error/banned")


class CodeUpdateRequest(BaseModel):
    """兑换码更新请求"""
    has_warranty: bool = Field(..., description="是否为质保兑换码")
    warranty_days: Optional[int] = Field(None, description="质保天数")

class BulkCodeUpdateRequest(BaseModel):
    """批量兑换码更新请求"""
    codes: List[str] = Field(..., description="兑换码列表")
    has_warranty: bool = Field(..., description="是否为质保兑换码")
    warranty_days: Optional[int] = Field(None, description="质保天数")


class BulkActionRequest(BaseModel):
    """批量操作请求"""
    ids: List[int] = Field(..., description="Team ID 列表")


@router.get("/", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    page: int = 1,
    per_page: int = 20,
    search: Optional[str] = None,
    status_filter: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    管理员面板首页
    """
    try:
        from app.main import templates
        logger.info(f"管理员访问控制台, search={search}, page={page}, per_page={per_page}")

        # 设置每页数量
        # per_page = 20 (Removed hardcoded value)
        
        # 获取 Team 列表 (分页)
        teams_result = await team_service.get_all_teams(db, page=page, per_page=per_page, search=search, status=status_filter, pool_type="normal")
        
        # 获取统计信息 (使用专用统计方法优化)
        team_stats = await team_service.get_stats(db, pool_type="normal")
        code_stats = await redemption_service.get_stats(db, pool_type="normal")

        # 计算统计数据
        stats = {
            "total_teams": team_stats["total"],
            "available_teams": team_stats["available"],
            "total_codes": code_stats["total"],
            "used_codes": code_stats["used"]
        }

        return templates.TemplateResponse(
            "admin/index.html",
            {
                "request": request,
                "user": current_user,
                "active_page": "dashboard",
                "ui_theme": await resolve_ui_theme(db),
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
            }
        )
    except Exception as e:
        logger.error(f"加载管理员面板失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"加载管理员面板失败: {str(e)}"
        )




@router.get("/welfare", response_class=HTMLResponse)
async def welfare_dashboard(
    request: Request,
    page: int = 1,
    per_page: int = 20,
    search: Optional[str] = None,
    status_filter: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """福利车位管理页"""
    try:
        from app.main import templates

        teams_result = await team_service.get_all_teams(db, page=page, per_page=per_page, search=search, status=status_filter, pool_type="welfare")
        team_stats = await team_service.get_stats(db, pool_type="welfare")
        remaining_spots = await team_service.get_total_available_seats(db, pool_type="welfare")
        welfare_code = await settings_service.get_setting(db, "welfare_common_code", "")
        welfare_limit_raw = await settings_service.get_setting(db, "welfare_common_code_limit", "0")
        welfare_used_raw = await settings_service.get_setting(db, "welfare_common_code_used_count", "0")

        # 福利通用码可用次数应与当前可用车位一致：sum(max_members - current_members)
        usable_capacity_stmt = select(func.sum(Team.max_members - Team.current_members)).where(
            Team.pool_type == "welfare",
            Team.status == "active",
            Team.current_members < Team.max_members
        )
        usable_capacity_result = await db.execute(usable_capacity_stmt)
        usable_capacity = int(usable_capacity_result.scalar() or 0)

        try:
            welfare_limit = int(str(welfare_limit_raw or "0").strip() or 0)
        except Exception:
            welfare_limit = 0
        try:
            welfare_used = int(str(welfare_used_raw or "0").strip() or 0)
        except Exception:
            welfare_used = 0

        # 兼容历史错误值：展示时按当前真实可用车位收敛
        effective_limit = usable_capacity if usable_capacity >= 0 else 0

        stats = {
            "total_teams": team_stats["total"],
            "available_teams": team_stats["available"],
            "remaining_spots": remaining_spots,
            "welfare_code": welfare_code,
            "welfare_code_limit": effective_limit,
            "welfare_code_used": welfare_used,
            "welfare_code_remaining": max(effective_limit - welfare_used, 0),
        }

        return templates.TemplateResponse(
            "admin/index.html",
            {
                "request": request,
                "user": current_user,
                "active_page": "welfare",
                "ui_theme": await resolve_ui_theme(db),
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
            }
        )
    except Exception as e:
        logger.error(f"加载福利车位页面失败: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"加载福利车位页面失败: {str(e)}")


@router.post("/welfare/code/generate")
async def generate_welfare_common_code(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """生成/更新福利通用兑换码（不落库到 redemption_codes，仅存 settings）。"""
    try:
        seats_stmt = select(func.sum(Team.max_members - Team.current_members)).where(
            Team.pool_type == "welfare",
            Team.status == "active",
            Team.current_members < Team.max_members
        )
        seats_result = await db.execute(seats_stmt)
        total_seats = int(seats_result.scalar() or 0)

        if total_seats <= 0:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "error": "当前没有可用的福利车位，无法生成通用兑换码"}
            )

        code = redemption_service._generate_random_code()

        # 兼容清理：历史版本可能把福利通用码写入 redemption_codes，这里统一失效处理
        await db.execute(
            update(RedemptionCode)
            .where(RedemptionCode.pool_type == "welfare", RedemptionCode.reusable_by_seat == True)
            .values(status="expired")
        )
        await db.commit()

        # 每次生成新码都会立即替换旧码（旧码自动失效）
        await settings_service.update_settings(db, {
            "welfare_common_code": code,
            "welfare_common_code_limit": str(total_seats),
            "welfare_common_code_used_count": "0",
            "welfare_common_code_generated_at": get_now().isoformat()
        })

        return JSONResponse(content={"success": True, "code": code, "limit": total_seats, "used": 0, "remaining": total_seats})
    except Exception as e:
        logger.error(f"生成福利通用兑换码失败: {e}")
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"success": False, "error": str(e)})

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
        logger.error(f"删除 Team 失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": f"删除 Team 失败: {str(e)}"
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
            content={"success": False, "error": str(e)}
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
            content={"success": False, "error": str(e)}
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
        logger.error(f"导入 Team 失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": f"导入失败: {str(e)}"
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
        logger.error(f"生成 OAuth 授权链接失败: {e}")
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"success": False, "error": str(e)})


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
        logger.error(f"解析 OAuth 回调失败: {e}")
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"success": False, "error": str(e)})


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
        logger.error(f"获取成员列表失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": f"获取成员列表失败: {str(e)}"
            }
        )


@router.post("/teams/{team_id}/members/add")
async def add_team_member(
    team_id: int,
    member_data: AddMemberRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    添加 Team 成员

    Args:
        team_id: Team ID
        member_data: 成员数据
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        添加结果
    """
    try:
        logger.info(f"管理员添加成员到 Team {team_id}: {member_data.email}")

        result = await team_service.add_team_member(
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
        logger.error(f"添加成员失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": f"添加成员失败: {str(e)}"
            }
        )


@router.post("/teams/{team_id}/members/{user_id}/delete")
async def delete_team_member(
    team_id: int,
    user_id: str,
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
            db_session=db
        )

        if not result["success"]:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result
            )

        return JSONResponse(content=result)

    except Exception as e:
        logger.error(f"删除成员失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": f"删除成员失败: {str(e)}"
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
        logger.error(f"撤回邀请失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": f"撤回邀请失败: {str(e)}"
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
        logger.error(f"开启设备身份验证失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": f"操作失败: {str(e)}"
            }
        )


# ==================== 批量操作路由 ====================

@router.post("/teams/batch-refresh")
async def batch_refresh_teams(
    action_data: BulkActionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    批量刷新 Team 信息
    """
    try:
        logger.info(f"管理员批量刷新 {len(action_data.ids)} 个 Team")
        
        success_count = 0
        failed_count = 0
        
        for team_id in action_data.ids:
            try:
                # 注意: 这里使用 sync_team_info, 它会自动处理 Token 刷新和信息同步
                # force_refresh=True 代表强制同步 API
                result = await team_service.sync_team_info(team_id, db, force_refresh=True)
                if result.get("success"):
                    success_count += 1
                else:
                    failed_count += 1
            except Exception as ex:
                logger.error(f"批量刷新 Team {team_id} 时出错: {ex}")
                failed_count += 1
        
        return JSONResponse(content={
            "success": True,
            "message": f"批量刷新完成: 成功 {success_count}, 失败 {failed_count}",
            "success_count": success_count,
            "failed_count": failed_count
        })
    except Exception as e:
        logger.error(f"批量刷新 Team 失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": str(e)}
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
        logger.error(f"批量删除 Team 失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": str(e)}
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
        logger.error(f"批量处理失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": str(e)}
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

        return templates.TemplateResponse(
            "admin/codes/index.html",
            {
                "request": request,
                "user": current_user,
                "active_page": "codes",
                "ui_theme": await resolve_ui_theme(db),
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
            }
        )

    except Exception as e:
        logger.error(f"加载兑换码列表页面失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"加载页面失败: {str(e)}"
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
        logger.error(f"生成兑换码失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": f"生成失败: {str(e)}"
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
        logger.error(f"删除兑换码失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": f"删除失败: {str(e)}"
            }
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
        logger.error(f"导出兑换码失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"导出失败: {str(e)}"
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
            content={"success": False, "error": str(e)}
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
            content={"success": False, "error": str(e)}
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

        return templates.TemplateResponse(
            "admin/records/index.html",
            {
                "request": request,
                "user": current_user,
                "active_page": "records",
                "ui_theme": await resolve_ui_theme(db),
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
            }
        )

    except Exception as e:
        logger.error(f"获取使用记录失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取使用记录失败: {str(e)}"
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
        logger.error(f"撤回记录失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": f"撤回失败: {str(e)}"
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

        return templates.TemplateResponse(
            "admin/settings/index.html",
            {
                "request": request,
                "user": current_user,
                "active_page": "settings",
                "ui_theme": await resolve_ui_theme(db),
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
                "default_team_max_members": await settings_service.get_setting(db, "default_team_max_members", "6"),
                "warranty_expiration_mode": await settings_service.get_warranty_expiration_mode(db),
                "ui_theme": settings_service.normalize_ui_theme(await settings_service.get_setting(db, "ui_theme", DEFAULT_UI_THEME)),
            }
        )

    except Exception as e:
        logger.error(f"获取系统设置失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取系统设置失败: {str(e)}"
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


class TeamAutoRefreshSettingsRequest(BaseModel):
    """Team 自动刷新设置请求"""
    enabled: bool = Field(True, description="是否启用 Team 周期状态自动刷新")
    interval_hours: int = Field(12, ge=1, le=168, description="检查间隔（小时）")
    refresh_interval_days: int = Field(7, ge=1, le=30, description="同步周期（天）")


class WarrantyExpirationSettingsRequest(BaseModel):
    """质保时长计算模式设置请求"""
    expiration_mode: Literal["first_use", "refresh_on_redeem"] = Field(
        DEFAULT_WARRANTY_EXPIRATION_MODE,
        description="质保时长计算模式"
    )


class UiThemeSettingsRequest(BaseModel):
    """系统配色设置请求"""
    theme: Literal["ocean", "warm"] = Field(DEFAULT_UI_THEME, description="系统配色主题")


class AnnouncementUpdateRequest(BaseModel):
    """公告配置请求"""
    enabled: bool = Field(False, description="是否启用公告")
    markdown: str = Field("", description="公告 Markdown 内容")




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
        logger.error(f"更新系统配色失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": f"更新失败: {str(e)}"}
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

        return templates.TemplateResponse(
            "admin/announcement/index.html",
            {
                "request": request,
                "user": current_user,
                "active_page": "announcement",
                "ui_theme": await resolve_ui_theme(db),
                "announcement_enabled": announcement_enabled,
                "announcement_markdown": announcement_markdown,
            }
        )
    except Exception as e:
        logger.error(f"获取公告设置失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取公告设置失败: {str(e)}"
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
        logger.error(f"保存公告设置失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": f"保存失败: {str(e)}"}
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

        logger.info(f"管理员更新代理配置: enabled={proxy_data.enabled}, proxy={proxy_data.proxy}")

        # 验证代理地址格式
        if proxy_data.enabled and proxy_data.proxy:
            proxy = proxy_data.proxy.strip()
            if not (proxy.startswith("http://") or proxy.startswith("https://") or proxy.startswith("socks5://") or proxy.startswith("socks5h://")):
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={
                        "success": False,
                        "error": "代理地址格式错误,应为 http://host:port, socks5://host:port 或 socks5h://host:port"
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
        logger.error(f"更新代理配置失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": f"更新失败: {str(e)}"}
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
        logger.error(f"更新日志级别失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": f"更新失败: {str(e)}"}
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
        logger.error(f"更新配置失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": f"更新失败: {str(e)}"}
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
        logger.error(f"更新 Token 自动刷新设置失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": f"更新失败: {str(e)}"}
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
        logger.error(f"更新 Team 自动刷新设置失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": f"更新失败: {str(e)}"}
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
        logger.error(f"更新质保设置失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": f"更新失败: {str(e)}"}
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
        logger.error(f"更新 Team 导入设置失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": f"更新失败: {str(e)}"}
        )
