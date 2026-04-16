"""
Team 管理服务
用于管理 Team 账号的导入、同步、成员管理等功能
"""
import logging
import json
import asyncio
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
import pytz
from sqlalchemy import select, update, delete, func, or_, case
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Team, TeamAccount, RedemptionCode, TeamEmailMapping
from app.services.chatgpt import ChatGPTService
from app.services.encryption import encryption_service
from app.utils.token_parser import TokenParser
from app.utils.jwt_parser import JWTParser
from app.utils.time_utils import get_now
from app.config import settings

logger = logging.getLogger(__name__)

TEAM_EMAIL_STATUS_INVITED = "invited"
TEAM_EMAIL_STATUS_JOINED = "joined"
TEAM_EMAIL_STATUS_REMOVED = "removed"
TEAM_EMAIL_SYNC_MISS_THRESHOLD = 3
ACTIVE_TEAM_EMAIL_STATUSES = (
    TEAM_EMAIL_STATUS_INVITED,
    TEAM_EMAIL_STATUS_JOINED,
)


class TeamService:
    """Team 管理服务类"""

    PROACTIVE_REFRESH_WINDOW_HOURS = 2
    PLACEHOLDER_ACCOUNT_IDS = {"default", "personal", "none", "null", "me", "self"}

    def __init__(self):
        """初始化 Team 管理服务"""
        from app.services.chatgpt import chatgpt_service
        self.chatgpt_service = chatgpt_service
        self.token_parser = TokenParser()
        self.jwt_parser = JWTParser()

    def _parse_remote_expires_at(self, expires_at_raw: Optional[str]) -> Optional[datetime]:
        """将 OpenAI 返回的 expires_at 解析为本地时区语义的 naive datetime。"""
        if not expires_at_raw:
            return None

        try:
            normalized = str(expires_at_raw).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is not None:
                local_tz = pytz.timezone(settings.timezone)
                return dt.astimezone(local_tz).replace(tzinfo=None)
            return dt
        except Exception as e:
            logger.warning(f"解析过期时间失败: {e}")
            return None

    def _normalize_account_id(self, account_id: Optional[str]) -> Optional[str]:
        """清理 OAuth 回调中常见的占位 account_id（如 default）。"""
        value = str(account_id or "").strip()
        if not value:
            return None
        if value.lower() in self.PLACEHOLDER_ACCOUNT_IDS:
            return None
        return value

    async def _hydrate_missing_id_token(
        self,
        *,
        db_session: AsyncSession,
        access_token: Optional[str],
        refresh_token: Optional[str],
        session_token: Optional[str],
        client_id: Optional[str],
        account_id: Optional[str],
        identifier: str,
        id_token: Optional[str] = None,
    ) -> Dict[str, Optional[str]]:
        """
        在 code flow 未直接返回 id_token 时，优先通过 RT/ST 补刷一次，把 id_token 持久化所需数据补全。
        失败时不阻断导入/更新，仅返回当前可用数据。
        """
        hydrated = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "session_token": session_token,
            "id_token": id_token,
        }

        if hydrated["id_token"]:
            return hydrated

        normalized_client_id = str(client_id or "").strip()
        normalized_account_id = self._normalize_account_id(account_id)

        if refresh_token and normalized_client_id:
            refresh_result = await self.chatgpt_service.refresh_access_token_with_refresh_token(
                refresh_token,
                normalized_client_id,
                db_session,
                identifier=identifier,
            )
            if refresh_result.get("success"):
                hydrated["access_token"] = refresh_result.get("access_token") or hydrated["access_token"]
                hydrated["refresh_token"] = refresh_result.get("refresh_token") or hydrated["refresh_token"]
                hydrated["id_token"] = refresh_result.get("id_token") or hydrated["id_token"]
                if hydrated["id_token"]:
                    logger.info("通过 refresh_token 成功补齐 id_token")
                    return hydrated
            else:
                logger.warning("通过 refresh_token 补齐 id_token 失败: %s", refresh_result.get("error"))

        if session_token:
            refresh_result = await self.chatgpt_service.refresh_access_token_with_session_token(
                session_token,
                db_session,
                account_id=normalized_account_id,
                identifier=identifier,
            )
            if refresh_result.get("success"):
                hydrated["access_token"] = refresh_result.get("access_token") or hydrated["access_token"]
                hydrated["session_token"] = refresh_result.get("session_token") or hydrated["session_token"]
                hydrated["id_token"] = refresh_result.get("id_token") or hydrated["id_token"]
                if hydrated["id_token"]:
                    logger.info("通过 session_token 成功补齐 id_token")
                    return hydrated
            else:
                logger.warning("通过 session_token 补齐 id_token 失败: %s", refresh_result.get("error"))

        return hydrated

    async def reserve_seat_if_available(
        self,
        team_id: int,
        db_session: AsyncSession,
        pool_type: str = "normal"
    ) -> Dict[str, Any]:
        """
        以数据库原子更新的方式预留一个席位。

        这样即使在多 worker / 多实例环境中，也不会仅依赖进程内锁导致超拉。
        """
        current_time = get_now()
        reserve_stmt = (
            update(Team)
            .where(
                Team.id == team_id,
                Team.pool_type == pool_type,
                Team.status == "active",
                Team.current_members < Team.max_members,
                or_(Team.expires_at.is_(None), Team.expires_at >= current_time),
            )
            .values(
                current_members=Team.current_members + 1,
                status=case(
                    (Team.current_members + 1 >= Team.max_members, "full"),
                    else_="active",
                ),
            )
        )
        reserve_result = await db_session.execute(reserve_stmt)
        if (reserve_result.rowcount or 0) <= 0:
            team = await db_session.get(Team, team_id)
            if not team:
                return {"success": False, "error": f"目标 Team {team_id} 不存在"}
            if team.pool_type != pool_type:
                return {"success": False, "error": f"目标 Team {team_id} 不属于当前兑换池"}
            if team.expires_at and team.expires_at < current_time:
                team.status = "expired"
                await db_session.flush()
                return {"success": False, "error": f"目标 Team {team_id} 已过期"}
            if team.current_members >= team.max_members:
                team.status = "full"
                await db_session.flush()
                return {"success": False, "error": "该 Team 已满, 请选择其他 Team 尝试"}
            return {
                "success": False,
                "error": f"目标 Team {team_id} 不可用 ({team.status})"
            }

        team = await db_session.get(Team, team_id)
        if not team:
            return {"success": False, "error": f"目标 Team {team_id} 不存在"}

        return {"success": True, "team": team, "error": None}

    async def release_reserved_seat(
        self,
        team_id: int,
        db_session: AsyncSession,
        pool_type: str = "normal"
    ) -> None:
        """释放一次已预留的席位，并回写 Team 状态。"""
        team = await db_session.get(Team, team_id)
        if not team or team.pool_type != pool_type:
            return

        if team.current_members > 0:
            team.current_members -= 1

        if team.current_members >= team.max_members:
            team.status = "full"
        elif team.expires_at and team.expires_at < get_now():
            team.status = "expired"
        else:
            team.status = "active"

    async def _handle_api_error(self, result: Dict[str, Any], team: Team, db_session: AsyncSession) -> bool:
        """
        检查结果是否表示账号被封禁、Token 失效或 Team 已满,如果是则更新状态
        
        Returns:
            bool: 是否已处理致命错误
        """
        error_code = result.get("error_code")
        error_msg = str(result.get("error", "")).lower()
        
        # 1. 判定是否为“封号/永久失效”类致命错误
        # 明确的错误码匹配
        ban_codes = {
            "account_deactivated", 
            "token_invalidated", 
            "account_suspended", 
            "account_not_found",
            "user_not_found",
            "deactivated_workspace"
        }
        is_banned = error_code in ban_codes
        
        # 关键词匹配 (针对不同接口返回的文本差异，尤其是刷新 Token 时 descripton 里的信息)
        if not is_banned:
            ban_keywords = [
                "token has been invalidated", 
                "account_deactivated",
                "account has been deactivated",
                "account is deactivated",
                "account_suspended",
                "account is suspended",
                "account was deleted",
                "user_not_found",
                "this account is deactivated",
                "deactivated_workspace"
            ]
            if any(kw in error_msg for kw in ban_keywords):
                is_banned = True
                
        # session_token 失效不等于账号封禁，允许后续走 refresh_token 刷新
        if "session_invalidated" in error_msg:
            logger.info(f"Team {team.id} session_token 已失效，继续尝试其他刷新方式")
            return False

        # 1.1 判定是否为“虚假成功” (Ghost Success)
        if error_code == "ghost_success":
            logger.error(f"检测到 Team {team.id} ({team.email}) 存在“虚假成功”现象 (邀请返回 200 但列表无成员)，标记为 error")
            team.status = "error"
            await db_session.commit()
            return True

        if is_banned:
            # 简化状态描述判断
            if "workspace" in error_msg or "workspace" in (error_code or ""):
                status_desc = "到期"
            elif any(x in error_msg for x in ["deactivated", "suspended", "not found", "deleted"]):
                status_desc = "封禁"
            else:
                status_desc = "失效"
                
            logger.warning(f"检测到账号{status_desc} (code={error_code}, msg={error_msg}), 更新 Team {team.id} ({team.email}) 状态为 banned")
            team.status = "banned"
            await db_session.commit()
            return True

        # 2. 判定是否为“席位已满”错误
        full_keywords = ["maximum number of seats", "reached maximum number of seats", "no seats available"]
        if any(kw in error_msg for kw in full_keywords):
            logger.warning(f"检测到 Team 席位已满 (msg={error_msg}), 更新 Team {team.id} ({team.email}) 状态为 full")
            team.status = "full"
            # 学习真实的席位上限: 如果当前探测到的成员数小于预设的最大值，说明该团队实际容量较小
            if team.current_members > 0 and team.current_members < team.max_members:
                logger.info(f"修正 Team {team.id} 的最大成员数: {team.max_members} -> {team.current_members}")
                team.max_members = team.current_members
            elif team.current_members >= team.max_members:
                # 进位修正，确保逻辑闭环
                team.current_members = team.max_members

            await db_session.commit()
            return True

        # 2.5 判定是否为“已在团队中” (这通常被视为成功的变种)
        already_in_keywords = ["already in workspace", "already in team", "already a member"]
        if any(kw in error_msg for kw in already_in_keywords):
            logger.info(f"Team {team.id} 提示用户已在团队中: {error_msg}")
            # 虽然提示错误，但在业务逻辑上应视为加入成功
            return False # 返回 False 表示不是致命故障，允许后续逻辑（如下车/同步）继续

        # 3. 判定是否为 Token 过期 (需刷新)
        is_token_expired = error_code == "token_expired" or "token_expired" in error_msg or "token is expired" in error_msg
        
        # 4. 处理其他所有非致命错误 (累加错误次数)
        # 只要走到这里，说明不是封号也不是满员，统统记录错误
        logger.warning(f"Team {team.id} ({team.email}) 请求出错 (code={error_code}, msg={error_msg})")
        
        team.error_count = (team.error_count or 0) + 1
        if team.error_count >= 3:
            # 如果错误次数达标且是 Token 问题，标记为 expired 提高可读性
            if is_token_expired:
                logger.error(f"Team {team.id} 连续 Token 错误，标记为 expired")
                team.status = "expired"
            else:
                logger.error(f"Team {team.id} 连续错误 {team.error_count} 次，标记为 error")
                team.status = "error"
        
        # 如果是 Token 过期，尝试立即刷新一次（为下次重试做准备）
        if is_token_expired:
            logger.info(f"Team {team.id} Token 过期，尝试后台刷新...")
            # 注意：此处会根据刷新结果立即修正状态，避免前端仍显示 active
            refreshed_token = await self.ensure_access_token(team, db_session)
            if not refreshed_token and team.status != "banned":
                logger.error(f"Team {team.id} Token 过期且刷新失败，立即标记为 expired")
                team.status = "expired"

        await db_session.commit()
        return True
        
    @staticmethod
    def _admin_error(error_code: str, error: str, message: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
        """为管理员页面构造结构化错误响应。"""
        payload: Dict[str, Any] = {
            "success": False,
            "message": message,
            "error_code": error_code,
            "error": error,
        }
        payload.update(extra)
        return payload

    async def _reset_error_status(self, team: Team, db_session: AsyncSession) -> None:
        """
        成功执行请求后重置错误计数并尝试从 error 状态恢复
        """
        team.error_count = 0
        if team.status == "error":
            # 恢复时也要校验是否满员或到期
            if team.current_members >= team.max_members:
                logger.info(f"Team {team.id} ({team.email}) 请求成功, 将状态从 error 恢复为 full")
                team.status = "full"
            elif team.expires_at and team.expires_at < get_now():
                logger.info(f"Team {team.id} ({team.email}) 请求成功, 将状态从 error 恢复为 expired")
                team.status = "expired"
            else:
                logger.info(f"Team {team.id} ({team.email}) 请求成功, 将状态从 error 恢复为 active")
                team.status = "active"
        await db_session.commit()

    @staticmethod
    def _normalize_member_email(email: Optional[str]) -> Optional[str]:
        """统一邮箱格式，避免大小写和空白差异导致重复记录。"""
        if not email:
            return None
        normalized = str(email).strip().lower()
        return normalized or None

    def _filter_pending_invites(
        self,
        invite_items: Optional[List[Dict[str, Any]]],
        joined_emails: Optional[set[str]] = None,
    ) -> List[Dict[str, Any]]:
        """过滤仍占位的待加入邀请，并剔除与已加入成员重复的邮箱。"""
        normalized_joined = {
            email for email in (joined_emails or set()) if email
        }
        active_invite_statuses = {"pending", "invited", "sent", "open", "active"}
        pending_items: List[Dict[str, Any]] = []

        for invite in invite_items or []:
            email = self._normalize_member_email(
                invite.get("email_address") or invite.get("email")
            )
            if not email or email in normalized_joined:
                continue

            raw_status = str(
                invite.get("status")
                or invite.get("invite_status")
                or invite.get("state")
                or ""
            ).strip().lower()
            if raw_status and raw_status not in active_invite_statuses:
                continue

            pending_items.append({**invite, "email_address": email})

        return pending_items

    async def _get_active_mapping_count(self, team_id: int, db_session: AsyncSession) -> int:
        """统计本地仍占位的成员/邀请数量，用作同步时的人数下限。"""
        result = await db_session.execute(
            select(func.count(TeamEmailMapping.id)).where(
                TeamEmailMapping.team_id == team_id,
                TeamEmailMapping.status.in_(ACTIVE_TEAM_EMAIL_STATUSES),
            )
        )
        return int(result.scalar() or 0)

    async def _apply_member_count_floor(
        self,
        team: Team,
        observed_member_count: int,
        db_session: AsyncSession,
    ) -> int:
        """将远端同步人数与本地仍活跃占位合并，避免短暂同步延迟把人数回写变小。"""
        await db_session.flush()
        active_mapping_count = await self._get_active_mapping_count(team.id, db_session)
        observed_count = int(observed_member_count or 0)
        effective_members = max(observed_count, active_mapping_count)
        if team.max_members and team.max_members > 0:
            effective_members = min(effective_members, team.max_members)

        team.current_members = effective_members
        if team.expires_at and team.expires_at < get_now():
            team.status = "expired"
        elif team.current_members >= team.max_members:
            team.status = "full"
        else:
            team.status = "active"

        return effective_members

    async def get_active_team_ids_for_email(
        self,
        email: str,
        db_session: AsyncSession,
        pool_type: Optional[str] = None
    ) -> List[int]:
        """
        获取某邮箱当前仍占用中的 Team 列表。
        基于本地映射表判定，不访问远端接口。
        """
        normalized_email = self._normalize_member_email(email)
        if not normalized_email:
            return []

        stmt = (
            select(TeamEmailMapping.team_id)
            .join(Team, TeamEmailMapping.team_id == Team.id)
            .where(
                TeamEmailMapping.email == normalized_email,
                TeamEmailMapping.status.in_(ACTIVE_TEAM_EMAIL_STATUSES),
                Team.status.in_(["active", "full"]),
                or_(Team.expires_at.is_(None), Team.expires_at >= get_now()),
            )
            .order_by(TeamEmailMapping.team_id.asc())
        )
        if pool_type:
            stmt = stmt.where(Team.pool_type == pool_type)

        result = await db_session.execute(stmt)
        team_ids = result.scalars().all()
        return list(dict.fromkeys(team_ids))

    async def upsert_team_email_mapping(
        self,
        team_id: int,
        email: str,
        status: str,
        db_session: AsyncSession,
        source: str = "sync",
        seen_at: Optional[datetime] = None
    ) -> Optional[TeamEmailMapping]:
        """创建或更新 Team-邮箱映射。"""
        normalized_email = self._normalize_member_email(email)
        if not normalized_email:
            return None

        current_time = seen_at or get_now()
        stmt = select(TeamEmailMapping).where(
            TeamEmailMapping.team_id == team_id,
            TeamEmailMapping.email == normalized_email
        )
        result = await db_session.execute(stmt)
        mapping = result.scalar_one_or_none()

        if not mapping:
            mapping = TeamEmailMapping(
                team_id=team_id,
                email=normalized_email,
                status=status,
                source=source,
                last_seen_at=current_time,
                missing_sync_count=0,
            )
            db_session.add(mapping)
            return mapping

        mapping.status = status
        mapping.source = source
        mapping.last_seen_at = current_time
        mapping.missing_sync_count = 0
        return mapping

    async def mark_team_email_mapping_removed(
        self,
        team_id: int,
        email: str,
        db_session: AsyncSession,
        source: str = "sync",
        seen_at: Optional[datetime] = None
    ) -> Optional[TeamEmailMapping]:
        """将 Team-邮箱映射标记为已移除。"""
        return await self.upsert_team_email_mapping(
            team_id=team_id,
            email=email,
            status=TEAM_EMAIL_STATUS_REMOVED,
            db_session=db_session,
            source=source,
            seen_at=seen_at,
        )

    async def _reconcile_team_email_mappings(
        self,
        team_id: int,
        joined_emails: set[str],
        invited_emails: set[str],
        db_session: AsyncSession
    ) -> None:
        """
        根据一次完整同步结果，重建某个 Team 的邮箱状态映射。
        """
        seen_at = get_now()
        normalized_joined = {
            email for email in (self._normalize_member_email(item) for item in joined_emails) if email
        }
        normalized_invited = {
            email for email in (self._normalize_member_email(item) for item in invited_emails) if email
        } - normalized_joined

        stmt = select(TeamEmailMapping).where(TeamEmailMapping.team_id == team_id)
        result = await db_session.execute(stmt)
        existing_mappings = {
            mapping.email: mapping
            for mapping in result.scalars().all()
            if mapping.email
        }

        for email in normalized_joined:
            mapping = existing_mappings.get(email)
            if mapping:
                mapping.status = TEAM_EMAIL_STATUS_JOINED
                mapping.source = "sync"
                mapping.last_seen_at = seen_at
                mapping.missing_sync_count = 0
            else:
                db_session.add(
                    TeamEmailMapping(
                        team_id=team_id,
                        email=email,
                        status=TEAM_EMAIL_STATUS_JOINED,
                        source="sync",
                        last_seen_at=seen_at,
                        missing_sync_count=0,
                    )
                )

        for email in normalized_invited:
            mapping = existing_mappings.get(email)
            if mapping:
                mapping.status = TEAM_EMAIL_STATUS_INVITED
                mapping.source = "sync"
                mapping.last_seen_at = seen_at
                mapping.missing_sync_count = 0
            else:
                db_session.add(
                    TeamEmailMapping(
                        team_id=team_id,
                        email=email,
                        status=TEAM_EMAIL_STATUS_INVITED,
                        source="sync",
                        last_seen_at=seen_at,
                        missing_sync_count=0,
                    )
                )

        active_emails = normalized_joined | normalized_invited
        for email, mapping in existing_mappings.items():
            if email not in active_emails and mapping.status in ACTIVE_TEAM_EMAIL_STATUSES:
                mapping.missing_sync_count = (mapping.missing_sync_count or 0) + 1
                if mapping.missing_sync_count >= TEAM_EMAIL_SYNC_MISS_THRESHOLD:
                    mapping.status = TEAM_EMAIL_STATUS_REMOVED
                    mapping.source = "sync"
                    mapping.last_seen_at = seen_at

    async def ensure_access_token(self, team: Team, db_session: AsyncSession, force_refresh: bool = False) -> Optional[str]:
        """
        确保 AT Token 有效,如果过期则尝试刷新
        
        Args:
            team: Team 对象
            db_session: 数据库会话
            force_refresh: 是否强制刷新 (忽略过期检查)
            
        Returns:
            有效的 AT Token, 刷新失败返回 None
        """
        current_valid_token: Optional[str] = None

        try:
            # 1. 解密当前 Token
            access_token = encryption_service.decrypt_token(team.access_token_encrypted)
            if access_token and not self.jwt_parser.is_token_expired(access_token):
                current_valid_token = access_token
            
            # 2. 检查是否过期 (如果不强制刷新且未过期，则返回)
            if not force_refresh and current_valid_token:
                return access_token
                
            if force_refresh:
                logger.info(f"Team {team.id} ({team.email}) 强制刷新 Token")
            else:
                logger.info(f"Team {team.id} ({team.email}) Token 已过期, 尝试刷新")
        except Exception as e:
            logger.error(f"解密或验证 Token 失败: {e}")
            access_token = None # 可能是解密失败，强制走刷新流程

        # 3. 优先使用 refresh_token 刷新（无 ST 场景更稳定）
        if team.refresh_token_encrypted:
            # 自动补齐 client_id（优先 Token claim，其次系统设置）
            if not team.client_id:
                try:
                    current_at = encryption_service.decrypt_token(team.access_token_encrypted)
                    token_client_id = self.jwt_parser.extract_client_id(current_at)
                    if token_client_id:
                        team.client_id = token_client_id
                        logger.info(f"Team {team.id} 从 AT 自动提取到 client_id")
                except Exception:
                    pass

                if not team.client_id:
                    from app.services.settings import settings_service
                    fallback_client_id = await settings_service.get_setting(db_session, "token_refresh_client_id", "")
                    if fallback_client_id:
                        team.client_id = fallback_client_id.strip()
                        logger.info(f"Team {team.id} 使用系统设置中的默认 client_id")

            if not team.client_id:
                logger.warning(f"Team {team.id} 存在 refresh_token 但缺少 client_id，跳过 RT 刷新")
            else:
                refresh_token = encryption_service.decrypt_token(team.refresh_token_encrypted)
                refresh_result = await self.chatgpt_service.refresh_access_token_with_refresh_token(
                    refresh_token, team.client_id, db_session, identifier=team.email
                )
                if refresh_result["success"]:
                    new_at = refresh_result["access_token"]
                    new_id_token = refresh_result.get("id_token")
                    new_rt = refresh_result.get("refresh_token")
                    logger.info(f"Team {team.id} 通过 refresh_token 成功刷新 AT")
                    team.access_token_encrypted = encryption_service.encrypt_token(new_at)
                    if new_id_token:
                        team.id_token_encrypted = encryption_service.encrypt_token(new_id_token)
                    if new_rt:
                        team.refresh_token_encrypted = encryption_service.encrypt_token(new_rt)
                    # 成功刷新，重置错误状态
                    await self._reset_error_status(team, db_session)
                    return new_at
                else:
                    # 检查是否为致命错误 (如 account_deactivated)
                    if await self._handle_api_error(refresh_result, team, db_session):
                        return None

        # 4. 兜底尝试使用 session_token 刷新
        if team.session_token_encrypted:
            session_token = encryption_service.decrypt_token(team.session_token_encrypted)
            refresh_result = await self.chatgpt_service.refresh_access_token_with_session_token(
                session_token, db_session, account_id=team.account_id, identifier=team.email
            )
            if refresh_result["success"]:
                new_at = refresh_result["access_token"]
                new_st = refresh_result.get("session_token")
                new_id_token = refresh_result.get("id_token")
                logger.info(f"Team {team.id} 通过 session_token 成功刷新 AT")
                team.access_token_encrypted = encryption_service.encrypt_token(new_at)
                if new_id_token:
                    team.id_token_encrypted = encryption_service.encrypt_token(new_id_token)

                # 如果返回了新的 session_token,予以更新
                if new_st and new_st != session_token:
                    logger.info(f"Team {team.id} Session Token 已更新")
                    team.session_token_encrypted = encryption_service.encrypt_token(new_st)

                # 成功刷新，重置错误状态
                await self._reset_error_status(team, db_session)
                return new_at
            else:
                # 检查是否为致命错误 (如 token_invalidated)
                if await self._handle_api_error(refresh_result, team, db_session):
                    return None

        # force_refresh 场景下，如果刷新链路失败但当前 AT 仍可用，则回退到当前 AT，
        # 避免“误判过期”导致状态被错误写成 expired。
        if force_refresh and current_valid_token:
            logger.warning(
                f"Team {team.id} 强制刷新失败，但现有 AT 仍有效，回退使用当前 Token"
            )
            return current_valid_token

        if team.status != "banned":
            logger.error(f"Team {team.id} Token 已过期且无法刷新，标记为 expired")
            team.status = "expired"
            team.error_count = (team.error_count or 0) + 1
        await db_session.commit()
        return None

    async def proactive_refresh_tokens(
        self,
        db_session: AsyncSession,
        refresh_window_hours: Optional[int] = None
    ) -> Dict[str, int]:
        """在 AT 过期前自动预刷新 Token。"""
        window_hours = refresh_window_hours or self.PROACTIVE_REFRESH_WINDOW_HOURS
        threshold_time = get_now() + timedelta(hours=window_hours)

        stmt = select(Team)
        result = await db_session.execute(stmt)
        teams = result.scalars().all()

        stats = {"total": len(teams), "refreshed": 0, "skipped": 0, "failed": 0}

        for team in teams:
            if team.status == "banned":
                stats["skipped"] += 1
                continue

            if not team.refresh_token_encrypted and not team.session_token_encrypted:
                stats["skipped"] += 1
                continue

            try:
                access_token = encryption_service.decrypt_token(team.access_token_encrypted)
                expire_at = self.jwt_parser.get_expiration_time(access_token)
            except Exception as e:
                logger.warning(f"Team {team.id} Token 解析失败,尝试直接刷新: {e}")
                expire_at = None

            if expire_at and expire_at > threshold_time:
                stats["skipped"] += 1
                continue

            new_token = await self.ensure_access_token(team, db_session, force_refresh=True)
            if new_token:
                stats["refreshed"] += 1
                logger.info(f"✅ Team {team.id} ({team.email}) Token 预刷新成功")
            else:
                stats["failed"] += 1
                logger.warning(f"❌ Team {team.id} ({team.email}) Token 预刷新失败")

        await db_session.commit()
        return stats

    async def import_team_single(
        self,
        access_token: Optional[str],
        db_session: AsyncSession,
        email: Optional[str] = None,
        account_id: Optional[str] = None,
        id_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        session_token: Optional[str] = None,
        client_id: Optional[str] = None,
        pool_type: str = "normal"
    ) -> Dict[str, Any]:
        """
        单个导入 Team

        Args:
            access_token: AT Token (可选,如果提供 RT/ST 可自动获取)
            db_session: 数据库会话
            email: 邮箱 (可选,如果不提供则从 Token 中提取)
            account_id: Account ID (可选,如果不提供则从 API 获取并导入所有活跃的)

        Returns:
            结果字典,包含 success, team_id (第一个导入的), message, error
        """
        try:
            from app.services.settings import settings_service
            account_id = self._normalize_account_id(account_id)

            # 0. 如果带有 refresh_token 但未传 client_id，先尝试自动补齐
            if refresh_token and not client_id:
                if access_token:
                    client_id = self.jwt_parser.extract_client_id(access_token)
                if not client_id:
                    fallback_client_id = await settings_service.get_setting(db_session, "token_refresh_client_id", "")
                    client_id = fallback_client_id.strip() if fallback_client_id else None
                if client_id:
                    logger.info("导入时自动补齐 client_id")

            # 1. 检查并尝试刷新 Token (如果 AT 缺失或过期)
            is_at_valid = False
            if access_token:
                try:
                    if not self.jwt_parser.is_token_expired(access_token):
                        is_at_valid = True
                except:
                    pass
            
            if not is_at_valid:
                logger.info("导入时 AT 缺失或过期, 尝试使用 ST/RT 刷新")

                # 尝试 session_token
                if session_token:
                    refresh_result = await self.chatgpt_service.refresh_access_token_with_session_token(
                        session_token, db_session, account_id=account_id, identifier=email or "import"
                    )
                    if refresh_result["success"]:
                        access_token = refresh_result["access_token"]
                        if refresh_result.get("id_token"):
                            id_token = refresh_result["id_token"]
                        # 导入时如果 ST 变了,更新变量以便后续保存
                        if refresh_result.get("session_token"):
                            session_token = refresh_result["session_token"]
                        is_at_valid = True
                        logger.info("导入时通过 session_token 成功获取 AT")
                
                # 尝试 refresh_token
                if not is_at_valid and refresh_token and client_id:
                    refresh_result = await self.chatgpt_service.refresh_access_token_with_refresh_token(
                        refresh_token, client_id, db_session, identifier=email or "import"
                    )
                    if refresh_result["success"]:
                        access_token = refresh_result["access_token"]
                        if refresh_result.get("id_token"):
                            id_token = refresh_result["id_token"]
                        # RT 刷新可能会返回新的 RT
                        if refresh_result.get("refresh_token"):
                            refresh_token = refresh_result["refresh_token"]
                        is_at_valid = True
                        logger.info("导入时通过 refresh_token 成功获取 AT")

            hydrated_tokens = await self._hydrate_missing_id_token(
                db_session=db_session,
                access_token=access_token,
                refresh_token=refresh_token,
                session_token=session_token,
                client_id=client_id,
                account_id=account_id,
                identifier=email or "import",
                id_token=id_token,
            )
            access_token = hydrated_tokens["access_token"]
            refresh_token = hydrated_tokens["refresh_token"]
            session_token = hydrated_tokens["session_token"]
            id_token = hydrated_tokens["id_token"]
            if access_token:
                try:
                    is_at_valid = not self.jwt_parser.is_token_expired(access_token)
                except Exception:
                    is_at_valid = False

            if not access_token or not is_at_valid:
                return {
                    "success": False,
                    "team_id": None,
                    "email": email,
                    "message": None,
                    "error": "缺少有效的 Access Token，且无法通过 Session/Refresh Token 刷新"
                }

            # 2. 如果没有提供邮箱,从 Token 中提取; 如果提供了,则校验是否匹配 (安全兜底)
            token_email = self.jwt_parser.extract_email(access_token)
            id_token_email = self.jwt_parser.extract_email(id_token) if id_token else None
            if not email:
                email = token_email or id_token_email
                if not email:
                    return {
                        "success": False,
                        "team_id": None,
                        "email": None,
                        "message": None,
                        "error": "无法从 Token 中提取邮箱,请手动提供邮箱"
                    }
            elif token_email and token_email.lower() != email.lower():
                logger.error(f"导入时 Token 邮箱不匹配: 预期 {email}, 实际 {token_email}")
                return {
                    "success": False,
                    "team_id": None,
                    "email": email,
                    "message": None,
                    "error": f"Token 对应的账号身份 ({token_email}) 与提供的邮箱 ({email}) 不符，导入已中止。请检查是否有其他账号正在登录导致 Session 污染。"
                }

            # 2. 尝试从 API 获取账户信息
            accounts_to_import = []
            team_accounts = []
            
            account_result = await self.chatgpt_service.get_account_info(
                access_token,
                db_session,
                identifier=email
            )
            
            if account_result["success"]:
                team_accounts = account_result["accounts"]
            else:
                logger.warning(f"导入时获取账户信息失败: {account_result['error']}")

            # 3. 确定要导入的账户列表
            if account_id:
                # 3.1 优先处理指定的 account_id 以获取其元数据
                found_account = next((acc for acc in team_accounts if acc["account_id"] == account_id), None)
                
                if found_account:
                    accounts_to_import.append(found_account)
                    logger.info(f"导入时找到指定的 account_id: {account_id}, 已获取真实元数据")
                else:
                    # 如果未找到或 API 失败，保底使用占位符
                    placeholder = {
                        "account_id": account_id,
                        "name": f"Team-{account_id[:8]}",
                        "plan_type": "team",
                        "subscription_plan": "unknown",
                        "expires_at": None,
                        "has_active_subscription": True
                    }
                    accounts_to_import.append(placeholder)
                    if not team_accounts:
                        team_accounts.append(placeholder)
                    logger.info(f"导入时未找到指定的 account_id: {account_id}, 使用占位符元数据")
            
            # 3.2 自动导入 API 返回的所有其他活跃账号 (多账号支持)
            for acc in team_accounts:
                if acc["has_active_subscription"]:
                    # 避免与指定的 account_id 重复
                    if not any(a["account_id"] == acc["account_id"] for a in accounts_to_import):
                        accounts_to_import.append(acc)

            # 3.3 如果此时依然没有任何账号可导入 (且没有指定 account_id)
            if not accounts_to_import and not account_id:
                if not account_result["success"]:
                    return {
                        "success": False,
                        "team_id": None,
                        "email": email,
                        "message": None,
                        "error": f"获取账户信息失败: {account_result['error']}"
                    }
                
                if not team_accounts:
                    return {
                        "success": False,
                        "team_id": None,
                        "email": email,
                        "message": None,
                        "error": "该 Token 没有关联任何 Team 账户"
                    }
                
                # 保底使用第一个
                accounts_to_import.append(team_accounts[0])

            # 4. 循环处理这些账户
            imported_ids = []
            skipped_ids = []
            
            for selected_account in accounts_to_import:
                # 检查是否已存在 (根据 account_id)
                stmt = select(Team).where(
                    Team.account_id == selected_account["account_id"],
                    Team.pool_type == pool_type
                )
                result = await db_session.execute(stmt)
                existing_team = result.scalar_one_or_none()

                if existing_team:
                    skipped_ids.append(selected_account["account_id"])
                    continue

                # 获取成员列表 (包含已加入和待加入)
                members_result = await self.chatgpt_service.get_members(
                    access_token,
                    selected_account["account_id"],
                    db_session
                )
                
                invites_result = await self.chatgpt_service.get_invites(
                    access_token,
                    selected_account["account_id"],
                    db_session
                )

                current_members = 0
                if members_result["success"]:
                    current_members += members_result["total"]
                if invites_result["success"]:
                    current_members += invites_result["total"]

                # 解析过期时间
                expires_at = self._parse_remote_expires_at(selected_account.get("expires_at"))

                # 获取账户设置 (包含 beta_settings)
                device_code_auth_enabled = False
                settings_result = await self.chatgpt_service.get_account_settings(
                    access_token,
                    selected_account["account_id"],
                    db_session,
                    identifier=email
                )
                if settings_result["success"]:
                    beta_settings = settings_result["data"].get("beta_settings", {})
                    device_code_auth_enabled = beta_settings.get("codex_device_code_auth", False)

                # 确定状态和最大成员数 (默认读取系统设置，异常时回退到 6)
                max_members_raw = await settings_service.get_setting(
                    db_session,
                    "default_team_max_members",
                    "6"
                )
                try:
                    max_members = int(str(max_members_raw or "6").strip() or 6)
                except (TypeError, ValueError):
                    logger.warning(f"默认总席位配置无效: {max_members_raw}, 已回退到 6")
                    max_members = 6

                if max_members < 1 or max_members > 100:
                    logger.warning(f"默认总席位超出允许范围: {max_members}, 已回退到 6")
                    max_members = 6

                status = "active"
                if current_members >= max_members:
                    status = "full"
                elif expires_at and expires_at < get_now():
                    status = "expired"

                # 加密 AT Token
                encrypted_token = encryption_service.encrypt_token(access_token)
                encrypted_id_token = encryption_service.encrypt_token(id_token) if id_token else None
                encrypted_rt = encryption_service.encrypt_token(refresh_token) if refresh_token else None
                encrypted_st = encryption_service.encrypt_token(session_token) if session_token else None

                # 创建 Team 记录
                team = Team(
                    email=email,
                    access_token_encrypted=encrypted_token,
                    id_token_encrypted=encrypted_id_token,
                    refresh_token_encrypted=encrypted_rt,
                    session_token_encrypted=encrypted_st,
                    client_id=client_id,
                    encryption_key_id="default",
                    account_id=selected_account["account_id"],
                    team_name=selected_account["name"],
                    plan_type=selected_account["plan_type"],
                    subscription_plan=selected_account["subscription_plan"],
                    expires_at=expires_at,
                    current_members=current_members,
                    max_members=max_members,
                    status=status,
                    account_role=selected_account.get("account_user_role"),
                    device_code_auth_enabled=device_code_auth_enabled,
                    last_sync=get_now(),
                    pool_type=pool_type
                )

                db_session.add(team)
                await db_session.flush()  # 获取 team.id

                # 创建 TeamAccount 记录 (保存所有 Team 账户)
                for acc in team_accounts:
                    team_account = TeamAccount(
                        team_id=team.id,
                        account_id=acc["account_id"],
                        account_name=acc["name"],
                        is_primary=(acc["account_id"] == selected_account["account_id"])
                    )
                    db_session.add(team_account)
                
                imported_ids.append(team.id)

            # 5. 返回结果总结
            if not imported_ids and skipped_ids:
                return {
                    "success": False,
                    "team_id": None,
                    "email": email,
                    "message": None,
                    "error": f"共发现 {len(skipped_ids)} 个 Team 账号,但均已在系统中"
                }
            
            if not imported_ids:
                return {
                    "success": False,
                    "team_id": None,
                    "email": email,
                    "message": None,
                    "error": "未发现可导入的 Team 账号"
                }

            await db_session.commit()

            message = f"成功导入 {len(imported_ids)} 个 Team 账号"
            if skipped_ids:
                message += f" (另有 {len(skipped_ids)} 个已存在)"

            logger.info(f"Team 导入成功: {email}, 共 {len(imported_ids)} 个账号")

            return {
                "success": True,
                "team_id": imported_ids[0],
                "email": email,
                "message": message,
                "error": None
            }

        except Exception as e:
            await db_session.rollback()
            logger.error(f"Team 导入失败: {e}")
            return {
                "success": False,
                "team_id": None,
                "email": email,
                "message": None,
                "error": f"导入失败: {str(e)}"
            }


    async def update_team(
        self,
        team_id: int,
        db_session: AsyncSession,
        access_token: Optional[str] = None,
        id_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        session_token: Optional[str] = None,
        client_id: Optional[str] = None,
        email: Optional[str] = None,
        account_id: Optional[str] = None,
        max_members: Optional[int] = None,
        team_name: Optional[str] = None,
        status: Optional[str] = None,
        pool_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        更新 Team 信息

        Args:
            team_id: Team ID
            db_session: 数据库会话
            access_token: 新的 AT Token (可选)
            refresh_token: 新的 RT Token (可选)
            session_token: 新的 ST Token (可选)
            client_id: 新的 Client ID (可选)
            email: 新的邮箱 (可选)
            account_id: 新的 Account ID (可选)
            max_members: 最大成员数 (可选)
            team_name: Team 名称 (可选)
            status: 状态 (可选)

        Returns:
            结果字典
        """
        try:
            # 1. 查询 Team (包含关联的 team_accounts)
            stmt = select(Team).where(Team.id == team_id).options(
                selectinload(Team.team_accounts)
            )
            result = await db_session.execute(stmt)
            team = result.scalar_one_or_none()

            if not team:
                return {"success": False, "error": f"Team ID {team_id} 不存在"}

            account_id = self._normalize_account_id(account_id)

            # 2. 更新属性
            if email:
                team.email = email
            
            if team_name is not None:
                team.team_name = team_name

            if account_id:
                team.account_id = account_id
                # 更新关联账户的主次状态
                for acc in team.team_accounts:
                    if acc.account_id == account_id:
                        acc.is_primary = True
                    else:
                        acc.is_primary = False

            # 3. 更新 Token（支持显式清空 ST/RT/client_id）
            if access_token is not None and access_token.strip():
                team.access_token_encrypted = encryption_service.encrypt_token(access_token.strip())

            if id_token is not None:
                id_token = id_token.strip()
                team.id_token_encrypted = encryption_service.encrypt_token(id_token) if id_token else None

            if refresh_token is not None:
                refresh_token = refresh_token.strip()
                team.refresh_token_encrypted = encryption_service.encrypt_token(refresh_token) if refresh_token else None

            if session_token is not None:
                session_token = session_token.strip()
                team.session_token_encrypted = encryption_service.encrypt_token(session_token) if session_token else None

            if client_id is not None:
                client_id = client_id.strip()
                team.client_id = client_id or None

            current_access_token = None
            current_refresh_token = None
            current_session_token = None
            if team.access_token_encrypted:
                try:
                    current_access_token = encryption_service.decrypt_token(team.access_token_encrypted)
                except Exception as e:
                    logger.warning(f"解密 Team {team_id} access_token 失败，跳过当前值复用: {e}")
            if team.refresh_token_encrypted:
                try:
                    current_refresh_token = encryption_service.decrypt_token(team.refresh_token_encrypted)
                except Exception as e:
                    logger.warning(f"解密 Team {team_id} refresh_token 失败，跳过当前值复用: {e}")
            if team.session_token_encrypted:
                try:
                    current_session_token = encryption_service.decrypt_token(team.session_token_encrypted)
                except Exception as e:
                    logger.warning(f"解密 Team {team_id} session_token 失败，跳过当前值复用: {e}")

            current_id_token = None
            if team.id_token_encrypted:
                try:
                    current_id_token = encryption_service.decrypt_token(team.id_token_encrypted)
                except Exception as e:
                    logger.warning(f"解密 Team {team_id} id_token 失败，视为缺失: {e}")

            should_hydrate_missing_id_token = id_token is None
            if should_hydrate_missing_id_token and not current_id_token:
                hydrated_tokens = await self._hydrate_missing_id_token(
                    db_session=db_session,
                    access_token=current_access_token,
                    refresh_token=current_refresh_token,
                    session_token=current_session_token,
                    client_id=team.client_id,
                    account_id=team.account_id,
                    identifier=team.email or f"team_{team_id}",
                    id_token=current_id_token,
                )
                new_access_token = hydrated_tokens["access_token"]
                new_refresh_token = hydrated_tokens["refresh_token"]
                new_session_token = hydrated_tokens["session_token"]
                new_id_token = hydrated_tokens["id_token"]

                if new_access_token and new_access_token != current_access_token:
                    team.access_token_encrypted = encryption_service.encrypt_token(new_access_token)
                if new_refresh_token and new_refresh_token != current_refresh_token:
                    team.refresh_token_encrypted = encryption_service.encrypt_token(new_refresh_token)
                if new_session_token and new_session_token != current_session_token:
                    team.session_token_encrypted = encryption_service.encrypt_token(new_session_token)
                if new_id_token:
                    team.id_token_encrypted = encryption_service.encrypt_token(new_id_token)

            # 4. 更新最大成员数
            if max_members is not None:
                team.max_members = max_members

            # 5. 更新状态
            if status:
                team.status = status
            
            # 自动维护 active/full/expired 状态 (仅当当前处于这三者之一或刚更新了 max_members/status)
            if team.status in ["active", "full", "expired"]:
                if team.current_members >= team.max_members:
                    team.status = "full"
                elif team.expires_at and team.expires_at < get_now():
                    team.status = "expired"
                else:
                    team.status = "active"

            await db_session.commit()


            logger.info(f"Team {team_id} 信息更新成功")
            return {"success": True, "message": "Team 信息更新成功"}

        except Exception:
            await db_session.rollback()
            logger.exception("更新 Team 失败")
            return {"success": False, "error": "更新失败，请稍后重试"}

    async def batch_transfer_pool(
        self,
        ids: List[int],
        target_pool_type: str,
        db_session: AsyncSession,
    ) -> Dict[str, Any]:
        """批量转移 Team 的池类型。"""
        try:
            normalized_ids: List[int] = []
            seen_ids = set()
            for team_id in ids:
                if not isinstance(team_id, int):
                    continue
                if team_id in seen_ids:
                    continue
                seen_ids.add(team_id)
                normalized_ids.append(team_id)

            if not normalized_ids:
                return {"success": False, "error": "请选择要转移的 Team"}

            normalized_target = "welfare" if target_pool_type == "welfare" else "normal"

            query_stmt = select(Team).where(Team.id.in_(normalized_ids))
            query_result = await db_session.execute(query_stmt)
            teams = {team.id: team for team in query_result.scalars().all()}

            success_count = 0
            skipped_count = 0
            failed_count = 0

            for team_id in normalized_ids:
                team = teams.get(team_id)
                if not team:
                    failed_count += 1
                    continue

                current_pool_type = (team.pool_type or "normal").strip().lower()
                if current_pool_type == normalized_target:
                    skipped_count += 1
                    continue

                team.pool_type = normalized_target
                success_count += 1

            await db_session.commit()

            return {
                "success": True,
                "message": f"批量转移完成: 成功 {success_count}, 跳过 {skipped_count}, 失败 {failed_count}",
                "success_count": success_count,
                "skipped_count": skipped_count,
                "failed_count": failed_count,
                "target_pool_type": normalized_target,
                "error": None,
            }
        except Exception:
            await db_session.rollback()
            logger.exception("批量转移 Team 池类型失败")
            return {"success": False, "error": "批量转移失败，请稍后重试"}

    async def get_team_info(self, team_id: int, db_session: AsyncSession) -> Dict[str, Any]:
        """获取 Team 详细信息 (含解密 Token)"""
        try:
            stmt = select(Team).where(Team.id == team_id)
            result = await db_session.execute(stmt)
            team = result.scalar_one_or_none()

            if not team:
                return {"success": False, "error": "Team 不存在"}

            # 解密 Token
            access_token = ""
            id_token = ""
            refresh_token = ""
            session_token = ""
            try:
                access_token = encryption_service.decrypt_token(team.access_token_encrypted)
            except Exception as e:
                logger.error(f"解密 Team {team_id} access_token 失败: {e}")
            try:
                if team.id_token_encrypted:
                    id_token = encryption_service.decrypt_token(team.id_token_encrypted)
            except Exception as e:
                logger.error(f"解密 Team {team_id} id_token 失败: {e}")
            try:
                if team.refresh_token_encrypted:
                    refresh_token = encryption_service.decrypt_token(team.refresh_token_encrypted)
            except Exception as e:
                logger.error(f"解密 Team {team_id} refresh_token 失败: {e}")
            try:
                if team.session_token_encrypted:
                    session_token = encryption_service.decrypt_token(team.session_token_encrypted)
            except Exception as e:
                logger.error(f"解密 Team {team_id} session_token 失败: {e}")

            return {
                "success": True,
                "team": {
                    "id": team.id,
                    "email": team.email,
                    "account_id": team.account_id,
                    "max_members": team.max_members,
                    "access_token": access_token,
                    "id_token": id_token,
                    "refresh_token": refresh_token,
                    "session_token": session_token,
                    "client_id": team.client_id or "",
                    "team_name": team.team_name,
                    "status": team.status,
                    "account_role": team.account_role,
                    "device_code_auth_enabled": team.device_code_auth_enabled
                }
            }
        except Exception:
            logger.exception("获取 Team 信息失败")
            return {"success": False, "error": "获取 Team 信息失败，请稍后重试"}

    async def import_team_batch(
        self,
        text: str,
        db_session: AsyncSession,
        pool_type: str = "normal"
    ):
        """
        批量导入 Team (流式返回进度)

        Args:
            text: 包含 Token、邮箱、Account ID 的文本
            db_session: 数据库会话

        Yields:
            各阶段进度的 Dict
        """
        try:
            # 1. 解析文本
            parsed_data = self.token_parser.parse_team_import_text(text)

            if not parsed_data:
                yield {
                    "type": "error",
                    "error": "未能从文本中提取任何 Token"
                }
                return

            # 1.1 按邮箱去重 (以前是按 AT，现在改为按邮箱，防止重复处理同一个账号)
            seen_emails = set()
            unique_data = []
            for item in parsed_data:
                token = item.get("token")
                email = item.get("email")
                
                # 如果没有显式邮箱，尝试从 Token 中提取
                if not email and token:
                    try:
                        extracted = self.jwt_parser.extract_email(token)
                        if extracted:
                            email = extracted
                            item["email"] = email
                    except:
                        pass
                
                # 确定排重键：优先使用邮箱(不区分大小写)，如果没有则退而求其次使用 Token
                dedup_key = email.lower() if email else token
                
                if dedup_key and dedup_key not in seen_emails:
                    seen_emails.add(dedup_key)
                    unique_data.append(item)
            
            parsed_data = unique_data
            total = len(parsed_data)
            yield {
                "type": "start",
                "total": total
            }

            # 2. 逐个导入
            success_count = 0
            failed_count = 0

            for i, data in enumerate(parsed_data):
                result = await self.import_team_single(
                    access_token=data.get("token"),
                    db_session=db_session,
                    email=data.get("email"),
                    account_id=data.get("account_id"),
                    id_token=data.get("id_token"),
                    refresh_token=data.get("refresh_token"),
                    session_token=data.get("session_token"),
                    client_id=data.get("client_id"),
                    pool_type=pool_type
                )

                if result["success"]:
                    success_count += 1
                else:
                    failed_count += 1

                yield {
                    "type": "progress",
                    "current": i + 1,
                    "total": total,
                    "success_count": success_count,
                    "failed_count": failed_count,
                    "last_result": {
                        "email": result.get("email") or data.get("email") or "未知",
                        "account_id": data.get("account_id", "未指定"),
                        "success": result["success"],
                        "team_id": result["team_id"],
                        "message": result["message"],
                        "error": result["error"]
                    }
                }

            logger.info(f"批量导入完成: 总数 {total}, 成功 {success_count}, 失败 {failed_count}")

            yield {
                "type": "finish",
                "total": total,
                "success_count": success_count,
                "failed_count": failed_count
            }

        except Exception as e:
            logger.error(f"批量导入失败: {e}")
            yield {
                "type": "error",
                "error": f"批量导入过程中发生异常: {str(e)}"
            }

    async def import_team_json(
        self,
        json_text: Optional[str],
        db_session: AsyncSession,
        pool_type: str = "normal"
    ):
        """从 JSON 文本批量导入 Team（流式返回进度）。"""
        try:
            if not json_text:
                yield {"type": "error", "error": "JSON 内容不能为空"}
                return

            try:
                payload = json.loads(json_text)
            except Exception as e:
                yield {"type": "error", "error": f"JSON 解析失败: {e}"}
                return

            raw_items = []
            if isinstance(payload, list):
                raw_items = payload
            elif isinstance(payload, dict):
                if isinstance(payload.get("teams"), list):
                    raw_items = payload.get("teams")
                else:
                    raw_items = [payload]
            else:
                yield {"type": "error", "error": "JSON 顶层必须是对象或数组"}
                return

            normalized_items = []
            for item in raw_items:
                if not isinstance(item, dict):
                    continue

                access_token = item.get("access_token") or item.get("token")
                refresh_token = item.get("refresh_token")
                session_token = item.get("session_token")

                if not any([access_token, refresh_token, session_token]):
                    continue

                normalized_items.append({
                    "access_token": access_token,
                    "id_token": item.get("id_token"),
                    "refresh_token": refresh_token,
                    "session_token": session_token,
                    "client_id": item.get("client_id"),
                    "email": item.get("email"),
                    "account_id": item.get("account_id")
                })

            if not normalized_items:
                yield {"type": "error", "error": "JSON 中未找到可导入的 Team 项（需包含 AT/RT/ST）"}
                return

            total = len(normalized_items)
            yield {"type": "start", "total": total}

            success_count = 0
            failed_count = 0
            for i, data in enumerate(normalized_items):
                result = await self.import_team_single(
                    access_token=data.get("access_token"),
                    db_session=db_session,
                    email=data.get("email"),
                    account_id=data.get("account_id"),
                    id_token=data.get("id_token"),
                    refresh_token=data.get("refresh_token"),
                    session_token=data.get("session_token"),
                    client_id=data.get("client_id"),
                    pool_type=pool_type
                )

                if result["success"]:
                    success_count += 1
                else:
                    failed_count += 1

                yield {
                    "type": "progress",
                    "current": i + 1,
                    "total": total,
                    "success_count": success_count,
                    "failed_count": failed_count,
                    "last_result": {
                        "email": result.get("email") or data.get("email") or "未知",
                        "account_id": data.get("account_id", "未指定"),
                        "success": result["success"],
                        "team_id": result["team_id"],
                        "message": result["message"],
                        "error": result["error"]
                    }
                }

            yield {
                "type": "finish",
                "total": total,
                "success_count": success_count,
                "failed_count": failed_count
            }
        except Exception as e:
            logger.error(f"JSON 导入失败: {e}")
            yield {"type": "error", "error": f"JSON 导入过程中发生异常: {str(e)}"}

    async def sync_team_info(
        self,
        team_id: int,
        db_session: AsyncSession,
        force_refresh: bool = False
    ) -> Dict[str, Any]:
        """
        同步单个 Team 的信息

        Args:
            team_id: Team ID
            db_session: 数据库会话
            force_refresh: 是否强制刷新 Token

        Returns:
            结果字典,包含 success, message, error
        """
        try:
            # 1. 查询 Team
            stmt = select(Team).where(Team.id == team_id)
            result = await db_session.execute(stmt)
            team = result.scalar_one_or_none()

            if not team:
                return self._admin_error(
                    "team_not_found",
                    f"未找到 ID 为 {team_id} 的 Team",
                )

            # 2. 确保 AT Token 有效
            access_token = await self.ensure_access_token(team, db_session, force_refresh=force_refresh)
            if not access_token:
                if team.status == "banned":
                    return self._admin_error(
                        "team_banned",
                        "该 Team 账号已被封禁或凭证已失效，请重新导入可用账号",
                    )

                if team.status != "expired":
                    team.status = "expired"
                    await db_session.commit()

                return self._admin_error(
                    "token_refresh_failed",
                    "该 Team 的登录凭证已过期，且自动刷新失败，请重新登录或重新导入",
                )

            # 2.5 校验 Token 所属用户是否正确 (安全兜底)
            token_email = self.jwt_parser.extract_email(access_token)
            if token_email and team.email and token_email.lower() != team.email.lower():
                logger.error(f"Team {team_id} Token 邮箱不匹配: 预期 {team.email}, 实际 {token_email}")
                return self._admin_error(
                    "token_identity_mismatch",
                    "刷新出的账号身份与当前 Team 绑定账号不一致，已停止同步以避免串号，请清理旧 Session 后重新导入",
                )

            # 3. 获取账户信息
            account_result = await self.chatgpt_service.get_account_info(
                access_token,
                db_session,
                identifier=team.email
            )

            if not account_result["success"]:
                # 如果是 Token 过期，尝试在此处自动重试一次
                error_msg_raw = str(account_result.get("error", "")).lower()
                is_token_expired = account_result.get("error_code") == "token_expired" or "token_expired" in error_msg_raw or "token is expired" in error_msg_raw

                # 调用通用的错误处理逻辑 (包含标记封禁、累计错误次数、后台刷新等)
                await self._handle_api_error(account_result, team, db_session)

                if is_token_expired:
                    logger.info(f"Team {team.id} 同步时发现 Token 过期，尝试立即刷新并重试...")
                    new_token = await self.ensure_access_token(team, db_session, force_refresh=True)
                    if new_token:
                        # 2.6 重试后的 AT 也需要校验身份 (安全兜底)
                        new_token_email = self.jwt_parser.extract_email(new_token)
                        if new_token_email and team.email and new_token_email.lower() != team.email.lower():
                            logger.error(f"Team {team_id} 重试刷新 Token 邮箱不匹配: 预期 {team.email}, 实际 {new_token_email}")
                            return self._admin_error(
                                "token_identity_mismatch",
                                "刷新后的账号身份与当前 Team 绑定账号不一致，已停止同步以避免串号，请重新导入该账号",
                            )

                        # 使用新 Token 再次尝试
                        account_result = await self.chatgpt_service.get_account_info(new_token, db_session, identifier=team.email)
                        if account_result["success"]:
                            logger.info(f"Team {team.id} 自动刷新 Token 后重试同步成功")
                        else:
                            # 刷新成功但请求依然失败，标记为过期/异常
                            logger.error(f"Team {team.id} Token 刷新成功但获取账户信息仍失败，标记为 expired")
                            team.status = "expired"
                            if not db_session.in_transaction():
                                await db_session.commit()
                            return self._admin_error(
                                "account_info_unavailable_after_refresh",
                                "凭证刷新成功，但仍无法拉取 Team 信息，请稍后重试；若持续失败，建议重新导入该账号",
                            )
                    else:
                        # 刷新失败，标记为过期
                        logger.error(f"Team {team.id} Token 刷新失败，标记为 expired")
                        team.status = "expired"
                        if not db_session.in_transaction():
                            await db_session.commit()
                        return self._admin_error(
                            "token_refresh_failed",
                            "该 Team 的登录凭证已过期，且自动刷新失败，请重新登录或重新导入",
                        )
                else:
                    # 其他非 Token 过期导致的错误
                    admin_error_code = "team_sync_failed"
                    error_msg = "同步 Team 信息失败，请稍后重试"
                    if account_result.get("error_code") == "account_deactivated":
                        admin_error_code = "team_banned"
                        error_msg = "该 Team 账号已被封禁，请更换账号或重新导入"
                    elif account_result.get("error_code") == "token_invalidated":
                        admin_error_code = "team_banned"
                        error_msg = "该 Team 凭证已失效，账号当前不可用，请重新导入"
                    elif team.status == "error":
                        admin_error_code = "team_marked_error"
                        error_msg = "该 Team 连续多次同步失败，已被标记为异常，请检查账号状态或代理配置"
                    elif team.status == "full":
                        admin_error_code = "team_full"
                        error_msg = "该 Team 当前席位已满，无法继续邀请新成员"

                    return self._admin_error(admin_error_code, error_msg)

            # 4. 查找当前使用的 account
            team_accounts = account_result["accounts"]
            current_account = None

            for acc in team_accounts:
                if acc["account_id"] == team.account_id:
                    current_account = acc
                    break

            if not current_account:
                # 如果当前 account_id 不存在,使用第一个活跃的
                for acc in team_accounts:
                    if acc["has_active_subscription"]:
                        current_account = acc
                        break

                if not current_account and team_accounts:
                    current_account = team_accounts[0]

            if not current_account:
                team.status = "error"
                await db_session.commit()
                return {
                    "success": False,
                    "message": None,
                    "error": "该 Token 没有关联任何 Team 账户"
                }

            # 5. 小并发获取只读信息 (成员、邀请、账户设置)
            members_result, invites_result, settings_result = await asyncio.gather(
                self.chatgpt_service.get_members(
                    access_token,
                    current_account["account_id"],
                    db_session,
                    identifier=team.email
                ),
                self.chatgpt_service.get_invites(
                    access_token,
                    current_account["account_id"],
                    db_session,
                    identifier=team.email
                ),
                self.chatgpt_service.get_account_settings(
                    access_token,
                    current_account["account_id"],
                    db_session,
                    identifier=team.email
                ),
            )

            joined_member_emails = set()
            invited_member_emails = set()
            all_member_emails = set()
            current_members = 0
            if members_result["success"]:
                current_members += members_result["total"]
                for m in members_result.get("members", []):
                    if m.get("email"):
                        normalized_email = self._normalize_member_email(m["email"])
                        if normalized_email:
                            joined_member_emails.add(normalized_email)
                            all_member_emails.add(normalized_email)
            else:
                # 检查是否封号或 Token 失效
                if await self._handle_api_error(members_result, team, db_session):
                    error_msg = members_result.get("error", "未知错误")
                    if members_result.get("error_code") == "account_deactivated":
                        error_msg = "账号已封禁 (account_deactivated)"
                    elif members_result.get("error_code") == "token_invalidated":
                        error_msg = "账号已封禁/失效 (token_invalidated)"

                    return {
                        "success": False,
                        "message": None,
                        "error": error_msg
                    }

                # 其他错误, 累加错误次数
                team.error_count = (team.error_count or 0) + 1
                if team.error_count >= 3:
                    logger.error(f"Team {team.id} 获取成员列表连续失败 {team.error_count} 次，更新状态为 error")
                    team.status = "error"
                await db_session.commit()
                return {
                    "success": False,
                    "message": None,
                    "error": f"获取成员列表失败: {members_result.get('error', '未知错误')} (错误次数: {team.error_count})"
                }

            if invites_result["success"]:
                pending_invites = self._filter_pending_invites(
                    invites_result.get("items", []),
                    joined_emails=joined_member_emails,
                )
                current_members += len(pending_invites)
                for inv in pending_invites:
                    normalized_email = inv.get("email_address")
                    if normalized_email:
                        invited_member_emails.add(normalized_email)
                        all_member_emails.add(normalized_email)
            else:
                # 检查是否封号或 Token 失效
                if await self._handle_api_error(invites_result, team, db_session):
                    error_msg = invites_result.get("error", "未知错误")
                    if invites_result.get("error_code") == "account_deactivated":
                        error_msg = "账号已封禁 (account_deactivated)"
                    elif invites_result.get("error_code") == "token_invalidated":
                        error_msg = "账号已封禁/失效 (token_invalidated)"

                    return {
                        "success": False,
                        "message": None,
                        "error": error_msg
                    }

                # 其他错误, 累加错误次数
                team.error_count = (team.error_count or 0) + 1
                if team.error_count >= 3:
                    logger.error(f"Team {team.id} 获取邀请列表连续失败 {team.error_count} 次，更新状态为 error")
                    team.status = "error"
                await db_session.commit()
                return {
                    "success": False,
                    "message": None,
                    "error": f"获取邀请列表失败: {invites_result.get('error', '未知错误')} (错误次数: {team.error_count})"
                }

            # 6. 解析过期时间
            expires_at = self._parse_remote_expires_at(current_account.get("expires_at"))

            # 7.5 使用并发请求返回的账户设置 (包含 beta_settings)
            device_code_auth_enabled = team.device_code_auth_enabled
            if settings_result["success"]:
                beta_settings = settings_result["data"].get("beta_settings", {})
                device_code_auth_enabled = beta_settings.get("codex_device_code_auth", False)

            # 7. 确定状态
            status = "active"
            if current_members >= team.max_members:
                status = "full"
            elif expires_at and expires_at < get_now():
                status = "expired"
            
            # 8. 更新 Team 信息
            team.account_id = current_account["account_id"]
            team.team_name = current_account["name"]
            team.plan_type = current_account["plan_type"]
            team.subscription_plan = current_account["subscription_plan"]
            team.account_role = current_account.get("account_user_role")
            team.expires_at = expires_at
            team.device_code_auth_enabled = device_code_auth_enabled
            team.error_count = 0  # 同步成功，重置错误次数
            team.last_sync = get_now()
            await self._reconcile_team_email_mappings(
                team.id,
                joined_member_emails,
                invited_member_emails,
                db_session
            )
            effective_members = await self._apply_member_count_floor(team, current_members, db_session)

            await db_session.commit()

            logger.info(f"Team 同步成功: ID {team_id}, 成员数 {effective_members}")

            return {
                "success": True,
                "message": f"同步成功,当前成员数: {effective_members}",
                "member_emails": list(all_member_emails),
                "error": None
            }

        except Exception as e:
            await db_session.rollback()
            logger.error(f"Team 同步失败: {e}")
            return {
                "success": False,
                "message": None,
                "error": f"同步失败: {str(e)}"
            }

    async def sync_teams_due_for_periodic_refresh(
        self,
        db_session: AsyncSession,
        refresh_interval_days: int = 7
    ) -> Dict[str, Any]:
        """按周期（默认 7 天）自动刷新 Team 状态。"""
        try:
            stmt = select(Team)
            result = await db_session.execute(stmt)
            teams = result.scalars().all()

            if not teams:
                return {
                    "success": True,
                    "total": 0,
                    "due": 0,
                    "synced": 0,
                    "failed": 0,
                    "skipped": 0,
                    "results": [],
                    "error": None
                }

            now = get_now()
            due_teams = []
            skipped = 0

            for team in teams:
                if team.status == "banned":
                    skipped += 1
                    continue

                base_time = team.last_sync or team.created_at

                # created_at 为空时保守处理为“需要同步”
                if not base_time:
                    due_teams.append(team)
                    continue

                next_refresh_at = base_time + timedelta(days=refresh_interval_days)
                if now >= next_refresh_at:
                    due_teams.append(team)
                else:
                    skipped += 1

            synced = 0
            failed = 0
            results = []

            for team in due_teams:
                sync_result = await self.sync_team_info(team.id, db_session)
                if sync_result["success"]:
                    synced += 1
                else:
                    failed += 1

                results.append({
                    "team_id": team.id,
                    "email": team.email,
                    "success": sync_result["success"],
                    "message": sync_result.get("message"),
                    "error": sync_result.get("error")
                })

            logger.info(
                "周期状态同步完成: total=%s due=%s synced=%s failed=%s skipped=%s interval_days=%s",
                len(teams), len(due_teams), synced, failed, skipped, refresh_interval_days
            )

            return {
                "success": True,
                "total": len(teams),
                "due": len(due_teams),
                "synced": synced,
                "failed": failed,
                "skipped": skipped,
                "results": results,
                "error": None
            }
        except Exception as e:
            logger.error(f"周期状态同步失败: {e}")
            return {
                "success": False,
                "total": 0,
                "due": 0,
                "synced": 0,
                "failed": 0,
                "skipped": 0,
                "results": [],
                "error": f"周期状态同步失败: {str(e)}"
            }

    async def sync_all_teams(
        self,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        同步所有 Team 的信息

        Args:
            db_session: 数据库会话

        Returns:
            结果字典,包含 success, total, success_count, failed_count, results
        """
        try:
            # 1. 查询所有 Team
            stmt = select(Team)
            result = await db_session.execute(stmt)
            teams = result.scalars().all()

            if not teams:
                return {
                    "success": True,
                    "total": 0,
                    "success_count": 0,
                    "failed_count": 0,
                    "results": [],
                    "error": None
                }

            # 2. 逐个同步
            results = []
            success_count = 0
            failed_count = 0

            for team in teams:
                result = await self.sync_team_info(team.id, db_session)

                if result["success"]:
                    success_count += 1
                else:
                    failed_count += 1

                results.append({
                    "team_id": team.id,
                    "email": team.email,
                    "success": result["success"],
                    "message": result["message"],
                    "error": result["error"]
                })

            logger.info(f"批量同步完成: 总数 {len(teams)}, 成功 {success_count}, 失败 {failed_count}")

            return {
                "success": True,
                "total": len(teams),
                "success_count": success_count,
                "failed_count": failed_count,
                "results": results,
                "error": None
            }

        except Exception as e:
            logger.error(f"批量同步失败: {e}")
            return {
                "success": False,
                "total": 0,
                "success_count": 0,
                "failed_count": 0,
                "results": [],
                "error": f"批量同步失败: {str(e)}"
            }

    async def get_team_members(
        self,
        team_id: int,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        获取 Team 成员列表

        Args:
            team_id: Team ID
            db_session: 数据库会话

        Returns:
            结果字典,包含 success, members, total, error
        """
        try:
            # 1. 查询 Team
            stmt = select(Team).where(Team.id == team_id)
            result = await db_session.execute(stmt)
            team = result.scalar_one_or_none()

            if not team:
                return {
                    "success": False,
                    "members": [],
                    "total": 0,
                    "error": f"Team ID {team_id} 不存在"
                }

            # 2. 确保 AT Token 有效
            access_token = await self.ensure_access_token(team, db_session)
            if not access_token:
                return {
                    "success": False,
                    "members": [],
                    "total": 0,
                    "error": "Token 已过期且无法刷新"
                }

            # 3. 调用 ChatGPT API 获取成员列表
            members_result = await self.chatgpt_service.get_members(
                access_token,
                team.account_id,
                db_session
            )

            if not members_result["success"]:
                # 检查是否封号或 Token 失效
                if await self._handle_api_error(members_result, team, db_session):
                    error_msg = members_result.get("error", "未知错误")
                    if members_result.get("error_code") == "account_deactivated":
                        error_msg = "账号已封禁 (account_deactivated)"
                    elif members_result.get("error_code") == "token_invalidated":
                        error_msg = "Token 已失效 (token_invalidated)"
                        
                    return {
                        "success": False,
                        "members": [],
                        "total": 0,
                        "error": error_msg
                    }

                return {
                    "success": False,
                    "members": [],
                    "total": 0,
                    "error": f"获取成员列表失败: {members_result['error']}"
                }

            # 4. 调用 ChatGPT API 获取邀请列表
            invites_result = await self.chatgpt_service.get_invites(
                access_token,
                team.account_id,
                db_session
            )
            
            if not invites_result["success"]:
                # 检查是否封号或 Token 失效
                if await self._handle_api_error(invites_result, team, db_session):
                    error_msg = invites_result.get("error", "未知错误")
                    if invites_result.get("error_code") == "account_deactivated":
                        error_msg = "账号已封禁 (account_deactivated)"
                    elif invites_result.get("error_code") == "token_invalidated":
                        error_msg = "Token 已失效 (token_invalidated)"
                        
                    return {
                        "success": False,
                        "members": [],
                        "total": 0,
                        "error": error_msg
                    }

            # 5. 合并列表并统一格式
            all_members = []
            joined_emails: set[str] = set()

            # 处理已加入成员
            for m in members_result["members"]:
                normalized_email = self._normalize_member_email(m.get("email"))
                if normalized_email:
                    joined_emails.add(normalized_email)
                all_members.append({
                    "user_id": m.get("id"),
                    "email": normalized_email or m.get("email"),
                    "name": m.get("name"),
                    "role": m.get("role"),
                    "added_at": m.get("created_time"),
                    "status": "joined"
                })

            # 处理待加入成员
            if invites_result["success"]:
                pending_invites = self._filter_pending_invites(
                    invites_result.get("items", []),
                    joined_emails=joined_emails,
                )
                for inv in pending_invites:
                    all_members.append({
                        "user_id": None, # 邀请还没有 user_id
                        "email": inv.get("email_address"),
                        "name": None,
                        "role": inv.get("role"),
                        "added_at": inv.get("created_time"),
                        "status": "invited"
                    })

            logger.info(f"获取 Team {team_id} 成员列表成功: 共 {len(all_members)} 个成员 (已加入: {members_result['total']})")

            # 6. 持久化最新人数，避免“查看成员/撤回时已拿到实时列表，但数据库人数仍旧值”
            live_member_count = len(all_members)
            team.last_sync = get_now()
            effective_members = await self._apply_member_count_floor(team, live_member_count, db_session)

            # 7. 请求成功，重置错误状态
            await self._reset_error_status(team, db_session)

            return {
                "success": True,
                "members": all_members,
                "total": effective_members,
                "error": None
            }

        except Exception:
            logger.exception("获取成员列表失败")
            return {
                "success": False,
                "members": [],
                "total": 0,
                "error": "获取成员列表失败，请稍后重试"
            }

    async def revoke_team_invite(
        self,
        team_id: int,
        email: str,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        撤回 Team 邀请

        Args:
            team_id: Team ID
            email: 邀请邮箱
            db_session: 数据库会话

        Returns:
            结果字典,包含 success, message, error
        """
        try:
            # 1. 查询 Team
            stmt = select(Team).where(Team.id == team_id)
            result = await db_session.execute(stmt)
            team = result.scalar_one_or_none()

            if not team:
                return self._admin_error(
                    "team_not_found",
                    f"未找到 ID 为 {team_id} 的 Team",
                )

            # 2. 确保 AT Token 有效
            access_token = await self.ensure_access_token(team, db_session)
            if not access_token:
                return self._admin_error(
                    "token_refresh_failed",
                    "该 Team 的登录凭证已过期，且自动刷新失败，请重新登录或重新导入",
                )

            # 3. 调用 ChatGPT API 撤回邀请
            revoke_result = await self.chatgpt_service.delete_invite(
                access_token,
                team.account_id,
                email,
                db_session,
                identifier=team.email
            )

            if not revoke_result["success"]:
                # 检查是否封号或 Token 失效
                if await self._handle_api_error(revoke_result, team, db_session):
                    error_msg = revoke_result.get("error", "未知错误")
                    if revoke_result.get("error_code") == "account_deactivated":
                        error_msg = "账号已封禁 (account_deactivated)"
                    elif revoke_result.get("error_code") == "token_invalidated":
                        error_msg = "Token 已失效 (token_invalidated)"
                        
                    return {
                        "success": False,
                        "message": None,
                        "error": error_msg
                    }

                return {
                    "success": False,
                    "message": None,
                    "error": f"撤回邀请失败: {revoke_result['error']}"
                }

            await self.mark_team_email_mapping_removed(team_id, email, db_session, source="api")

            # 4. 更新成员数 (不再手动 -1，同步最新数据)
            await self.sync_team_info(team_id, db_session)

            await db_session.commit()

            logger.info(f"撤回邀请成功: {email} from Team {team_id}")

            # 5. 请求成功，重置错误状态
            await self._reset_error_status(team, db_session)

            return {
                "success": True,
                "message": f"已撤回对 {email} 的邀请",
                "error": None
            }

        except Exception:
            await db_session.rollback()
            logger.exception("撤回邀请失败")
            return {
                "success": False,
                "message": None,
                "error": "撤回邀请失败，请稍后重试"
            }

    async def add_team_member(
        self,
        team_id: int,
        email: str,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        添加 Team 成员

        Args:
            team_id: Team ID
            email: 成员邮箱
            db_session: 数据库会话

        Returns:
            结果字典,包含 success, message, error
        """
        normalized_email = self._normalize_member_email(email)
        if not normalized_email:
            return self._admin_error(
                "invalid_email",
                "邮箱格式不正确",
            )

        try:
            # 1. 查询 Team
            stmt = select(Team).where(Team.id == team_id)
            result = await db_session.execute(stmt)
            team = result.scalar_one_or_none()

            if not team:
                return self._admin_error(
                    "team_not_found",
                    f"未找到 ID 为 {team_id} 的 Team",
                )

            # 2. 检查 Team 状态
            if team.status == "full":
                return self._admin_error(
                    "team_full",
                    "Team 已满,无法添加成员",
                )

            if team.status == "expired":
                return self._admin_error(
                    "team_expired",
                    "Team 已过期,无法添加成员",
                )

            # 3. 确保 AT Token 有效
            access_token = await self.ensure_access_token(team, db_session)
            if not access_token:
                return self._admin_error(
                    "token_refresh_failed",
                    "该 Team 的登录凭证已过期，且自动刷新失败，请重新登录或重新导入",
                )

            sync_result = await self.sync_team_info(team_id, db_session)
            if not sync_result.get("success"):
                return self._admin_error(
                    "team_sync_failed",
                    sync_result.get("error") or "拉取 Team 最新成员状态失败，请稍后重试",
                )

            member_emails = {
                self._normalize_member_email(item)
                for item in sync_result.get("member_emails", [])
                if self._normalize_member_email(item)
            }
            if normalized_email in member_emails:
                return {
                    "success": True,
                    "message": f"{normalized_email} 已在该 Team 中",
                    "error": None,
                    "status": "already_exists",
                    "email": normalized_email,
                }

            team = await db_session.get(Team, team_id)
            if not team:
                return self._admin_error("team_not_found", f"Team ID {team_id} 不存在")

            if team.current_members >= team.max_members or team.status == "full":
                team.status = "full"
                await db_session.commit()
                return self._admin_error(
                    "team_full",
                    "Team 已满,无法添加成员",
                )

            # 4. 调用 ChatGPT API 发送邀请
            invite_result = await self.chatgpt_service.send_invite(
                access_token,
                team.account_id,
                normalized_email,
                db_session,
                identifier=team.email
            )

            if not invite_result["success"]:
                # 检查是否封号或 Token 失效
                if await self._handle_api_error(invite_result, team, db_session):
                    error_msg = invite_result.get("error", "未知错误")
                    if invite_result.get("error_code") == "account_deactivated":
                        error_msg = "账号已封禁 (account_deactivated)"
                    elif invite_result.get("error_code") == "token_invalidated":
                        error_msg = "Token 已失效 (token_invalidated)"

                    return self._admin_error(
                        invite_result.get("error_code") or "invite_failed",
                        error_msg,
                    )

                return self._admin_error(
                    invite_result.get("error_code") or "invite_failed",
                    f"发送邀请失败: {invite_result['error']}",
                )

            invite_data = invite_result.get("data", {})
            if "account_invites" in invite_data and not invite_data.get("account_invites"):
                await self._handle_api_error(
                    {"success": False, "error": "官方拦截下发(响应空列表)", "error_code": "ghost_success"},
                    team,
                    db_session
                )
                return self._admin_error(
                    "ghost_success",
                    "Team账号受限: 官方拦截下发(响应空列表)，请检查账单/风控状态",
                )

            # 5. 更新成员数并二次校验邀请是否真的生效 (循环检测 3 次，防止接口返回 200 但实际延迟入库)
            is_verified = False
            for i in range(3):
                await asyncio.sleep(5)
                sync_res = await self.sync_team_info(team_id, db_session)
                member_emails = [m.lower() for m in sync_res.get("member_emails", [])]
                if normalized_email in member_emails:
                    is_verified = True
                    logger.info(f"Team {team_id} [add_member] 同步确认成功 (尝试第 {i+1} 次)")
                    break
                if i < 2:
                    logger.warning(f"Team {team_id} [add_member] 尚未见到成员 {normalized_email}，准备第 {i+2} 次重试...")

            if not is_verified:
                logger.error(f"检测到“虚假成功”: Team {team_id} 发送邀请返回成功，但经过 3 次同步校验均未见该邮箱 {normalized_email}")
                # 标记错误
                await self._handle_api_error({"success": False, "error": "邀请发送成功但同步列表未见成员", "error_code": "ghost_success"}, team, db_session)
                return self._admin_error(
                    "ghost_success",
                    "邀请发送成功但 3 次同步成员列表校验均失败，该 Team 账号可能存在延迟或异常。建议稍后手动同步。",
                )

            await db_session.commit()

            logger.info(f"添加成员成功: {normalized_email} -> Team {team_id}")

            # 6. 请求成功，重置错误状态
            await self._reset_error_status(team, db_session)

            return {
                "success": True,
                "message": f"邀请已发送到 {normalized_email}",
                "error": None,
                "status": "invited",
                "email": normalized_email,
            }

        except Exception:
            await db_session.rollback()
            logger.exception("添加成员失败")
            return self._admin_error(
                "invite_failed",
                "添加成员失败，请稍后重试",
            )

    async def add_team_members(
        self,
        team_id: int,
        emails: List[str],
        db_session: AsyncSession,
    ) -> Dict[str, Any]:
        """批量添加 Team 成员。"""
        submitted = list(emails or [])
        results: List[Dict[str, Any]] = []
        summary = {
            "submitted": len(submitted),
            "unique": 0,
            "invited": 0,
            "invalid": 0,
            "duplicate": 0,
            "already_exists": 0,
            "no_seat": 0,
            "failed": 0,
            "not_processed": 0,
        }

        normalized_candidates: List[str] = []
        seen = set()
        for raw_email in submitted:
            normalized_email = self._normalize_member_email(raw_email)
            display_email = str(raw_email or "").strip()
            if not normalized_email or not self.token_parser.validate_email_format(normalized_email):
                summary["invalid"] += 1
                results.append({
                    "email": display_email or normalized_email or "",
                    "success": False,
                    "status": "invalid_email",
                    "message": None,
                    "error": "邮箱格式不正确",
                })
                continue
            if normalized_email in seen:
                summary["duplicate"] += 1
                results.append({
                    "email": normalized_email,
                    "success": False,
                    "status": "duplicate",
                    "message": None,
                    "error": "重复邮箱，已跳过",
                })
                continue
            seen.add(normalized_email)
            normalized_candidates.append(normalized_email)

        summary["unique"] = len(normalized_candidates)
        if not normalized_candidates:
            message = "未发现可处理的有效邮箱"
            return {
                "success": False,
                "processed": True,
                "partial_success": False,
                "message": message,
                "summary": summary,
                "results": results,
                "error": None,
            }

        initial_sync = await self.sync_team_info(team_id, db_session)
        if not initial_sync.get("success"):
            return self._admin_error(
                initial_sync.get("error_code") or "team_sync_failed",
                initial_sync.get("error") or "拉取 Team 最新成员状态失败，请稍后重试",
                processed=False,
                partial_success=False,
                summary=summary,
                results=results,
            )

        team = await db_session.get(Team, team_id)
        if not team:
            return self._admin_error(
                "team_not_found",
                f"未找到 ID 为 {team_id} 的 Team",
                processed=False,
                partial_success=False,
                summary=summary,
                results=results,
            )

        existing_emails = {
            self._normalize_member_email(item)
            for item in initial_sync.get("member_emails", [])
            if self._normalize_member_email(item)
        }
        available_slots = max(int(team.max_members or 0) - int(team.current_members or 0), 0)
        queued_emails: List[str] = []

        for normalized_email in normalized_candidates:
            if normalized_email in existing_emails:
                summary["already_exists"] += 1
                results.append({
                    "email": normalized_email,
                    "success": False,
                    "status": "already_exists",
                    "message": None,
                    "error": "该邮箱已在当前 Team 中",
                })
                continue
            if available_slots <= 0:
                summary["no_seat"] += 1
                results.append({
                    "email": normalized_email,
                    "success": False,
                    "status": "no_seat",
                    "message": None,
                    "error": "Team 剩余席位不足",
                })
                continue
            queued_emails.append(normalized_email)
            available_slots -= 1

        stop_processing = False
        fatal_error_codes = {
            "team_not_found",
            "team_sync_failed",
            "token_refresh_failed",
            "team_banned",
            "account_deactivated",
            "token_invalidated",
        }

        for normalized_email in queued_emails:
            if stop_processing:
                summary["not_processed"] += 1
                results.append({
                    "email": normalized_email,
                    "success": False,
                    "status": "not_processed",
                    "message": None,
                    "error": "处理过程中 Team 状态异常，后续邮箱未继续执行",
                })
                continue

            item_result = await self.add_team_member(team_id, normalized_email, db_session)
            item_status = item_result.get("status") or ("invited" if item_result.get("success") else "failed")
            item_message = item_result.get("message")
            item_error = item_result.get("error")
            item_email = item_result.get("email") or normalized_email

            if item_status == "already_exists":
                summary["already_exists"] += 1
            elif item_result.get("success") and item_status == "invited":
                summary["invited"] += 1
            elif item_result.get("error_code") == "team_full":
                summary["no_seat"] += 1
                item_status = "no_seat"
                item_error = item_error or "Team 剩余席位不足"
            else:
                summary["failed"] += 1

            results.append({
                "email": item_email,
                "success": bool(item_result.get("success")),
                "status": item_status,
                "message": item_message,
                "error": item_error,
            })

            if item_result.get("error_code") in fatal_error_codes:
                stop_processing = True

        invited_count = summary["invited"]
        failed_count = summary["invalid"] + summary["duplicate"] + summary["already_exists"] + summary["no_seat"] + summary["failed"] + summary["not_processed"]
        partial_success = invited_count > 0 and failed_count > 0

        if invited_count > 0 and failed_count > 0:
            message = f"共处理 {summary['submitted']} 个邮箱：成功 {invited_count} 个，失败 {failed_count} 个"
        elif invited_count > 0:
            message = f"成功发送 {invited_count} 个邀请"
        elif failed_count > 0:
            message = f"共处理 {summary['submitted']} 个邮箱，均未发送成功"
        else:
            message = "没有需要处理的邮箱"

        return {
            "success": invited_count > 0,
            "processed": True,
            "partial_success": partial_success,
            "message": message,
            "summary": summary,
            "results": results,
            "error": None,
        }

    async def delete_team_member(
        self,
        team_id: int,
        user_id: str,
        db_session: AsyncSession,
        email: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        删除 Team 成员

        Args:
            team_id: Team ID
            user_id: 用户 ID (格式: user-xxx)
            db_session: 数据库会话

        Returns:
            结果字典,包含 success, message, error
        """
        try:
            # 1. 查询 Team
            stmt = select(Team).where(Team.id == team_id)
            result = await db_session.execute(stmt)
            team = result.scalar_one_or_none()

            if not team:
                return self._admin_error(
                    "team_not_found",
                    f"未找到 ID 为 {team_id} 的 Team",
                )

            # 2. 确保 AT Token 有效
            access_token = await self.ensure_access_token(team, db_session)
            if not access_token:
                return self._admin_error(
                    "token_refresh_failed",
                    "该 Team 的登录凭证已过期，且自动刷新失败，请重新登录或重新导入",
                )

            # 3. 调用 ChatGPT API 删除成员
            delete_result = await self.chatgpt_service.delete_member(
                access_token,
                team.account_id,
                user_id,
                db_session,
                identifier=team.email
            )

            if not delete_result["success"]:
                # 检查是否封号或 Token 失效
                if await self._handle_api_error(delete_result, team, db_session):
                    error_msg = delete_result.get("error", "未知错误")
                    if delete_result.get("error_code") == "account_deactivated":
                        error_msg = "账号已封禁 (account_deactivated)"
                    elif delete_result.get("error_code") == "token_invalidated":
                        error_msg = "Token 已失效 (token_invalidated)"
                        
                    return {
                        "success": False,
                        "message": None,
                        "error": error_msg
                    }

                return {
                    "success": False,
                    "message": None,
                    "error": f"删除成员失败: {delete_result['error']}"
                }

            if email:
                await self.mark_team_email_mapping_removed(team_id, email, db_session, source="api")

            # 4. 更新成员数 (不再手动 -1，同步最新数据)
            await self.sync_team_info(team_id, db_session)

            await db_session.commit()

            logger.info(f"删除成员成功: {user_id} from Team {team_id}")

            # 5. 请求成功，重置错误状态
            await self._reset_error_status(team, db_session)

            return {
                "success": True,
                "message": "成员已删除",
                "error": None
            }

        except Exception:
            await db_session.rollback()
            logger.exception("删除成员失败")
            return {
                "success": False,
                "message": None,
                "error": "删除成员失败，请稍后重试"
            }

    async def enable_device_code_auth(
        self,
        team_id: int,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        开启 Team 的设备代码身份验证
        """
        try:
            # 1. 查询 Team
            stmt = select(Team).where(Team.id == team_id)
            result = await db_session.execute(stmt)
            team = result.scalar_one_or_none()

            if not team:
                return {"success": False, "error": f"Team ID {team_id} 不存在"}

            # 2. 确保 AT Token 有效
            access_token = await self.ensure_access_token(team, db_session)
            if not access_token:
                return {"success": False, "error": "Token 已过期且无法刷新"}

            # 3. 调用 ChatGPT API 开启功能
            result = await self.chatgpt_service.toggle_beta_feature(
                access_token,
                team.account_id,
                "codex_device_code_auth",
                True,
                db_session,
                identifier=team.email
            )

            if not result["success"]:
                return {"success": False, "error": f"开启设备身份验证失败: {result.get('error', '未知错误')}"}

            # 更新数据库状态
            team.device_code_auth_enabled = True
            await db_session.commit()

            logger.info(f"Team {team_id} ({team.email}) 开启设备身份验证成功")
            return {"success": True, "message": "设备代码身份验证开启成功"}

        except Exception:
            logger.exception("开启设备身份验证失败")
            return {"success": False, "error": "开启设备身份验证失败，请稍后重试"}

    async def get_available_teams(
        self,
        db_session: AsyncSession,
        pool_type: str = "normal"
    ) -> Dict[str, Any]:
        """
        获取可用的 Team 列表 (用于用户兑换页面)

        Args:
            db_session: 数据库会话

        Returns:
            结果字典,包含 success, teams, error
        """
        try:
            # 查询 status='active' 且 current_members < max_members 的 Team
            stmt = select(Team).where(
                Team.status == "active",
                Team.current_members < Team.max_members,
                Team.pool_type == pool_type
            )
            result = await db_session.execute(stmt)
            teams = result.scalars().all()

            # 构建返回数据 (不包含敏感信息)
            team_list = []
            for team in teams:
                team_list.append({
                    "id": team.id,
                    "team_name": team.team_name,
                    "current_members": team.current_members,
                    "max_members": team.max_members,
                    "expires_at": team.expires_at.isoformat() if team.expires_at else None,
                    "subscription_plan": team.subscription_plan
                })

            logger.info(f"获取可用 Team 列表成功: 共 {len(team_list)} 个")

            return {
                "success": True,
                "teams": team_list,
                "error": None
            }

        except Exception:
            logger.exception("获取可用 Team 列表失败")
            return {
                "success": False,
                "teams": [],
                "error": "获取列表失败，请稍后重试"
            }




    async def get_team_by_id(
        self,
        team_id: int,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        根据 ID 获取 Team 详情

        Args:
            team_id: Team ID
            db_session: 数据库会话

        Returns:
            结果字典,包含 success, team, team_accounts, error
        """
        try:
            # 查询 Team (包含关联的 team_accounts)
            stmt = select(Team).where(Team.id == team_id).options(
                selectinload(Team.team_accounts)
            )
            result = await db_session.execute(stmt)
            team = result.scalar_one_or_none()

            if not team:
                return {
                    "success": False,
                    "team": None,
                    "team_accounts": [],
                    "error": f"Team ID {team_id} 不存在"
                }

            # 解密 Token
            access_token = ""
            id_token = ""
            refresh_token = ""
            session_token = ""
            try:
                if team.access_token_encrypted:
                    access_token = encryption_service.decrypt_token(team.access_token_encrypted)
            except Exception as e:
                logger.error(f"解密 Team {team_id} access_token 失败: {e}")
            try:
                if team.id_token_encrypted:
                    id_token = encryption_service.decrypt_token(team.id_token_encrypted)
            except Exception as e:
                logger.error(f"解密 Team {team_id} id_token 失败: {e}")
            try:
                if team.refresh_token_encrypted:
                    refresh_token = encryption_service.decrypt_token(team.refresh_token_encrypted)
            except Exception as e:
                logger.error(f"解密 Team {team_id} refresh_token 失败: {e}")
            try:
                if team.session_token_encrypted:
                    session_token = encryption_service.decrypt_token(team.session_token_encrypted)
            except Exception as e:
                logger.error(f"解密 Team {team_id} session_token 失败: {e}")

            # 构建返回数据
            team_data = {
                "id": team.id,
                "email": team.email,
                "account_id": team.account_id,
                "access_token": access_token,
                "id_token": id_token,
                "refresh_token": refresh_token,
                "session_token": session_token,
                "client_id": team.client_id or "",
                "team_name": team.team_name,
                "plan_type": team.plan_type,
                "subscription_plan": team.subscription_plan,
                "expires_at": team.expires_at.isoformat() if team.expires_at else None,
                "current_members": team.current_members,
                "max_members": team.max_members,
                "status": team.status,
                "account_role": team.account_role,
                "device_code_auth_enabled": team.device_code_auth_enabled,
                "last_sync": team.last_sync.isoformat() if team.last_sync else None,
                "created_at": team.created_at.isoformat() if team.created_at else None
            }

            team_accounts_data = []
            for acc in team.team_accounts:
                team_accounts_data.append({
                    "id": acc.id,
                    "account_id": acc.account_id,
                    "account_name": acc.account_name,
                    "is_primary": acc.is_primary
                })

            logger.info(f"获取 Team {team_id} 详情成功")

            return {
                "success": True,
                "team": team_data,
                "team_accounts": team_accounts_data,
                "error": None
            }

        except Exception:
            logger.exception("获取 Team 详情失败")
            return {
                "success": False,
                "team": None,
                "team_accounts": [],
                "error": "获取 Team 详情失败，请稍后重试"
            }

    async def get_all_teams(
        self,
        db_session: AsyncSession,
        page: int = 1,
        per_page: int = 20,
        search: Optional[str] = None,
        status: Optional[str] = None,
        pool_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        获取所有 Team 列表 (用于管理员页面)

        Args:
            db_session: 数据库会话
            page: 页码
            per_page: 每页数量
            search: 搜索关键词
            status: 状态过滤 (可选)

        Returns:
            结果字典,包含 success, teams, total, total_pages, current_page, error
        """
        try:
            # 1. 构建查询语句
            stmt = select(Team)
            
            # 2. 如果有搜索词,添加过滤条件
            if search:
                from sqlalchemy import or_, cast, String
                search_filter = f"%{search}%"
                stmt = stmt.where(
                    or_(
                        Team.email.ilike(search_filter),
                        Team.account_id.ilike(search_filter),
                        Team.team_name.ilike(search_filter),
                        cast(Team.id, String).ilike(search_filter)
                    )
                )

            # 3. 如果有状态过滤,添加过滤条件
            if status:
                stmt = stmt.where(Team.status == status)

            if pool_type:
                stmt = stmt.where(Team.pool_type == pool_type)

            # 4. 获取总数
            count_stmt = select(func.count()).select_from(stmt.subquery())
            count_result = await db_session.execute(count_stmt)
            total = count_result.scalar() or 0

            # 4. 计算分页
            import math
            total_pages = math.ceil(total / per_page) if total > 0 else 1
            if page < 1:
                page = 1
            if total_pages > 0 and page > total_pages:
                page = total_pages
            
            offset = (page - 1) * per_page

            # 5. 查询分页数据
            final_stmt = stmt.order_by(Team.created_at.desc()).limit(per_page).offset(offset)
            result = await db_session.execute(final_stmt)
            teams = result.scalars().all()

            # 构建返回数据
            team_list = []
            for team in teams:
                team_list.append({
                    "id": team.id,
                    "email": team.email,
                    "account_id": team.account_id,
                    "team_name": team.team_name,
                    "plan_type": team.plan_type,
                    "subscription_plan": team.subscription_plan,
                    "expires_at": team.expires_at.isoformat() if team.expires_at else None,
                    "current_members": team.current_members,
                    "max_members": team.max_members,
                    "status": team.status,
                    "device_code_auth_enabled": getattr(team, 'device_code_auth_enabled', False),
                    "last_sync": team.last_sync.isoformat() if team.last_sync else None,
                    "created_at": team.created_at.isoformat() if team.created_at else None,
                    "pool_type": getattr(team, "pool_type", "normal")
                })

            logger.info(f"获取所有 Team 列表成功: 第 {page} 页, 共 {len(team_list)} 个 / 总数 {total}")

            return {
                "success": True,
                "teams": team_list,
                "total": total,
                "total_pages": total_pages,
                "current_page": page,
                "error": None
            }

        except Exception as e:
            logger.error(f"获取所有 Team 列表失败: {e}")
            return {
                "success": False,
                "teams": [],
                "error": f"获取所有 Team 列表失败: {str(e)}"
            }

    async def remove_invite_or_member(
        self,
        team_id: int,
        email: str,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        撤回邀请或删除成员 (根据邮箱自动判断)

        Args:
            team_id: Team ID
            email: 目标邮箱
            db_session: 数据库会话

        Returns:
            结果字典
        """
        try:
            # 1. 获取最新成员和邀请列表
            members_result = await self.get_team_members(team_id, db_session)
            if not members_result["success"]:
                return members_result

            all_members = members_result["members"]
            
            # 2. 查找匹配的记录
            target = next((m for m in all_members if m["email"] == email), None)
            
            if not target:
                logger.warning(f"在 Team {team_id} 中未找到邮箱为 {email} 的成员或邀请")
                # get_team_members 已拿到实时成员/邀请列表，但历史记录可能已经落后。
                # 此时主动同步一次，确保 current_members/status 与远端保持一致，
                # 否则撤回记录等后续流程会删除本地 RedemptionRecord，却保留错误的人数统计。
                await self.sync_team_info(team_id, db_session)
                # 即使没找到也返回成功，以便上层逻辑继续更新记录
                return {"success": True, "message": "成员已不存在"}

            # 3. 根据状态执行删除
            if target["status"] == "joined":
                # 已加入，调用删除成员
                return await self.delete_team_member(team_id, target["user_id"], db_session, email=email)
            else:
                # 待加入，调用撤回邀请
                return await self.revoke_team_invite(team_id, email, db_session)

        except Exception:
            logger.exception("撤回邀请或删除成员时发生异常")
            return {"success": False, "error": "撤回或删除成员失败，请稍后重试"}

    async def delete_team(
        self,
        team_id: int,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        删除 Team

        Args:
            team_id: Team ID
            db_session: 数据库会话

        Returns:
            结果字典,包含 success, message, error
        """
        try:
            # 1. 查询 Team
            stmt = select(Team).where(Team.id == team_id)
            result = await db_session.execute(stmt)
            team = result.scalar_one_or_none()

            if not team:
                return self._admin_error(
                    "team_not_found",
                    f"未找到 ID 为 {team_id} 的 Team",
                )

            # 1.5 处理 RedemptionCode 关联 (置空)
            update_stmt = update(RedemptionCode).where(RedemptionCode.used_team_id == team_id).values(used_team_id=None)
            await db_session.execute(update_stmt)

            # 2. 删除 Team (级联删除 team_accounts 和 redemption_records)
            await db_session.delete(team)
            await db_session.commit()

            logger.info(f"删除 Team {team_id} 成功")

            return {
                "success": True,
                "message": "Team 已删除",
                "error": None
            }

        except Exception:
            await db_session.rollback()
            logger.exception("删除 Team 失败")
            return {
                "success": False,
                "message": None,
                "error": "删除 Team 失败，请稍后重试"
            }

    async def get_total_available_seats(
        self,
        db_session: AsyncSession,
        pool_type: str = "normal"
    ) -> int:
        """
        获取所有活跃 Team 的总剩余车位数
        """
        try:
            # 统计所有状态为 active 的 Team 的剩余位置
            stmt = select(func.sum(Team.max_members - Team.current_members)).where(
                Team.status == "active",
                Team.current_members < Team.max_members,
                Team.pool_type == pool_type
            )
            result = await db_session.execute(stmt)
            return result.scalar() or 0
        except Exception as e:
            logger.error(f"获取总可用车位数失败: {e}")
            return 0

    async def get_stats(
        self,
        db_session: AsyncSession,
        pool_type: Optional[str] = None
    ) -> Dict[str, int]:
        """获取 Team 统计信息"""
        try:
            # 总数
            total_stmt = select(func.count(Team.id))
            if pool_type:
                total_stmt = total_stmt.where(Team.pool_type == pool_type)
            total_result = await db_session.execute(total_stmt)
            total = total_result.scalar() or 0

            # 可用 Team 数 (状态为 active 且未满)
            available_stmt = select(func.count(Team.id)).where(
                Team.status == "active",
                Team.current_members < Team.max_members
            )
            if pool_type:
                available_stmt = available_stmt.where(Team.pool_type == pool_type)
            available_result = await db_session.execute(available_stmt)
            available = available_result.scalar() or 0

            live_stmt = select(func.count(Team.id)).where(Team.status.in_(("active", "full")))
            if pool_type:
                live_stmt = live_stmt.where(Team.pool_type == pool_type)
            live_result = await db_session.execute(live_stmt)
            live = live_result.scalar() or 0

            banned_stmt = select(func.count(Team.id)).where(Team.status == "banned")
            if pool_type:
                banned_stmt = banned_stmt.where(Team.pool_type == pool_type)
            banned_result = await db_session.execute(banned_stmt)
            banned = banned_result.scalar() or 0

            expired_stmt = select(func.count(Team.id)).where(Team.status == "expired")
            if pool_type:
                expired_stmt = expired_stmt.where(Team.pool_type == pool_type)
            expired_result = await db_session.execute(expired_stmt)
            expired = expired_result.scalar() or 0

            return {
                "total": total,
                "available": available,
                "live": live,
                "banned": banned,
                "expired": expired,
            }
        except Exception as e:
            logger.error(f"获取 Team 统计信息失败: {e}")
            return {"total": 0, "available": 0, "live": 0, "banned": 0, "expired": 0}


# 创建全局 Team 服务实例
team_service = TeamService()
