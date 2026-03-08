"""
Team 管理服务
用于管理 Team 账号的导入、同步、成员管理等功能
"""
import logging
import asyncio
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from sqlalchemy import select, update, delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Team, TeamAccount, RedemptionCode, RedemptionRecord
from app.services.chatgpt import ChatGPTService
from app.services.encryption import encryption_service
from app.utils.token_parser import TokenParser
from app.utils.jwt_parser import JWTParser
from app.utils.time_utils import get_now
from app.config import settings
from app.services.member_lifecycle import member_lifecycle_service

logger = logging.getLogger(__name__)


class TeamService:
    """Team 管理服务类"""

    def __init__(self):
        """初始化 Team 管理服务"""
        from app.services.chatgpt import chatgpt_service
        self.chatgpt_service = chatgpt_service
        self.token_parser = TokenParser()
        self.jwt_parser = JWTParser()

    async def _handle_api_error(
        self,
        result: Dict[str, Any],
        team: Team,
        db_session: AsyncSession,
        stop_on_nonfatal: bool = True
    ) -> bool:
        """
        检查结果是否表示账号被封禁、Token 失效或 Team 已满,如果是则更新状态
        
        Returns:
            bool: 是否应中断当前流程
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
                "session_invalidated",
                "this account is deactivated",
                "deactivated_workspace"
            ]
            if any(kw in error_msg for kw in ban_keywords):
                is_banned = True
                
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
        full_keywords = ["maximum number of seats", "reached maximum number of seats"]
        if any(kw in error_msg for kw in full_keywords):
            logger.warning(f"检测到 Team 席位已满 (msg={error_msg}), 更新 Team {team.id} ({team.email}) 状态为 full")
            team.status = "full"
            # 修正当前成员数以防万一
            if team.current_members < team.max_members:
                team.current_members = team.max_members
            await db_session.commit()
            return True

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
        
        # 仅在普通 API 请求流程中，Token 过期时尝试后台刷新一次
        # 刷新流程内部失败时不再递归触发刷新，避免死循环
        if is_token_expired and stop_on_nonfatal:
            logger.info(f"Team {team.id} Token 过期，尝试后台刷新...")
            # 注意：此处不等待刷新结果，仅作为修复尝试
            await self.ensure_access_token(team, db_session)
            
        await db_session.commit()
        return stop_on_nonfatal
        
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
            elif team.expires_at and team.expires_at < datetime.now():
                logger.info(f"Team {team.id} ({team.email}) 请求成功, 将状态从 error 恢复为 expired")
                team.status = "expired"
            else:
                logger.info(f"Team {team.id} ({team.email}) 请求成功, 将状态从 error 恢复为 active")
                team.status = "active"
        await db_session.commit()

    def _should_proactive_refresh(self, team: Team) -> bool:
        """
        判断是否需要在后台提前刷新 Token
        """
        try:
            if not team.access_token_encrypted:
                return True
            access_token = encryption_service.decrypt_token(team.access_token_encrypted)
            exp_time = self.jwt_parser.get_expiration_time(access_token)
            if not exp_time:
                return True
            now = get_now()
            lead = max(0, int(settings.token_refresh_lead_seconds or 0))
            return (exp_time - now).total_seconds() <= lead
        except Exception:
            return True

    async def proactive_refresh_due_tokens(self, db_session: AsyncSession) -> Dict[str, Any]:
        """
        扫描并提前刷新即将过期的 Team Token（后台任务使用）
        """
        stmt = select(Team).where(Team.status != "banned")
        result = await db_session.execute(stmt)
        teams = result.scalars().all()

        checked = 0
        refreshed = 0
        failed = 0

        for team in teams:
            checked += 1
            if not self._should_proactive_refresh(team):
                continue

            token = await self.ensure_access_token(team, db_session, force_refresh=True)
            if token:
                refreshed += 1
            else:
                failed += 1

        return {
            "success": True,
            "checked": checked,
            "refreshed": refreshed,
            "failed": failed
        }

    def _is_token_expired_error(self, result: Dict[str, Any]) -> bool:
        """判断接口结果是否属于 Token 过期类错误"""
        error_msg = str(result.get("error", "")).lower()
        error_code = str(result.get("error_code", "")).lower()
        return (
            error_code == "token_expired"
            or "token_expired" in error_msg
            or "token is expired" in error_msg
            or result.get("status_code") == 401
        )

    async def _request_with_refresh_retry(
        self,
        team: Team,
        db_session: AsyncSession,
        request_func
    ) -> Dict[str, Any]:
        """
        请求级双保险：若本次请求报 Token 过期，则强制刷新后重试一次
        """
        access_token = await self.ensure_access_token(team, db_session)
        if not access_token:
            return {"success": False, "error": "Token 已过期且无法刷新", "error_code": "token_expired"}

        first = await request_func(access_token)
        if first.get("success"):
            return first

        if not self._is_token_expired_error(first):
            return first

        logger.info(f"Team {team.id} 请求触发 Token 过期，强制刷新并重试一次")
        refreshed_token = await self.ensure_access_token(team, db_session, force_refresh=True)
        if not refreshed_token:
            return first

        second = await request_func(refreshed_token)
        if second.get("success"):
            logger.info(f"Team {team.id} 刷新后重试请求成功")
        return second

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
        try:
            # 1. 解密当前 Token
            access_token = encryption_service.decrypt_token(team.access_token_encrypted)
            
            # 2. 检查是否过期 (如果不强制刷新且未过期，则返回)
            if not force_refresh and not self.jwt_parser.is_token_expired(access_token):
                return access_token
                
            if force_refresh:
                logger.info(f"Team {team.id} ({team.email}) 强制刷新 Token")
            else:
                logger.info(f"Team {team.id} ({team.email}) Token 已过期, 尝试刷新")
        except Exception as e:
            logger.error(f"解密或验证 Token 失败: {e}")
            access_token = None # 可能是解密失败，强制走刷新流程

        # 3. 尝试使用 session_token 刷新
        if team.session_token_encrypted:
            session_token = encryption_service.decrypt_token(team.session_token_encrypted)
            refresh_result = await self.chatgpt_service.refresh_access_token_with_session_token(
                session_token, db_session, account_id=team.account_id, identifier=team.email
            )
            if refresh_result["success"]:
                new_at = refresh_result["access_token"]
                new_st = refresh_result.get("session_token")
                logger.info(f"Team {team.id} 通过 session_token 成功刷新 AT")
                team.access_token_encrypted = encryption_service.encrypt_token(new_at)
                
                # 如果返回了新的 session_token,予以更新
                if new_st and new_st != session_token:
                    logger.info(f"Team {team.id} Session Token 已更新")
                    team.session_token_encrypted = encryption_service.encrypt_token(new_st)
                
                # 成功刷新，重置错误状态
                await self._reset_error_status(team, db_session)
                return new_at
            else:
                # 检查是否为致命错误 (如 token_invalidated)
                if await self._handle_api_error(refresh_result, team, db_session, stop_on_nonfatal=False):
                    return None

        # 4. 尝试使用 refresh_token 刷新
        if team.refresh_token_encrypted and team.client_id:
            refresh_token = encryption_service.decrypt_token(team.refresh_token_encrypted)
            refresh_result = await self.chatgpt_service.refresh_access_token_with_refresh_token(
                refresh_token, team.client_id, db_session, identifier=team.email
            )
            if refresh_result["success"]:
                new_at = refresh_result["access_token"]
                new_rt = refresh_result.get("refresh_token")
                logger.info(f"Team {team.id} 通过 refresh_token 成功刷新 AT")
                team.access_token_encrypted = encryption_service.encrypt_token(new_at)
                if new_rt:
                    team.refresh_token_encrypted = encryption_service.encrypt_token(new_rt)
                # 成功刷新，重置错误状态
                await self._reset_error_status(team, db_session)
                return new_at
            else:
                # 检查是否为致命错误 (如 account_deactivated)
                if await self._handle_api_error(refresh_result, team, db_session, stop_on_nonfatal=False):
                    return None
        
        if team.status != "banned":
            logger.error(f"Team {team.id} Token 已过期且无法刷新，标记为 expired")
            team.status = "expired"
            team.error_count = (team.error_count or 0) + 1
        await db_session.commit()
        return None

    async def import_team_single(
        self,
        access_token: Optional[str],
        db_session: AsyncSession,
        email: Optional[str] = None,
        account_id: Optional[str] = None,
        refresh_token: Optional[str] = None,
        session_token: Optional[str] = None,
        client_id: Optional[str] = None
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
                        # RT 刷新可能会返回新的 RT
                        if refresh_result.get("refresh_token"):
                            refresh_token = refresh_result["refresh_token"]
                        is_at_valid = True
                        logger.info("导入时通过 refresh_token 成功获取 AT")

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
            if not email:
                email = token_email
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
                    Team.account_id == selected_account["account_id"]
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
                expires_at = None
                if selected_account["expires_at"]:
                    try:
                        # ISO 8601 格式: 2026-02-21T23:10:05+00:00
                        expires_at = datetime.fromisoformat(
                            selected_account["expires_at"].replace("+00:00", "")
                        )
                    except Exception as e:
                        logger.warning(f"解析过期时间失败: {e}")

                # 确定状态
                # 新导入 Team 时还没有创建 ORM team 实例，这里应使用默认席位数
                max_members = 6
                status = "active"
                if current_members >= max_members:
                    status = "full"
                elif expires_at and expires_at < datetime.now():
                    status = "expired"

                # 加密 AT Token
                encrypted_token = encryption_service.encrypt_token(access_token)
                encrypted_rt = encryption_service.encrypt_token(refresh_token) if refresh_token else None
                encrypted_st = encryption_service.encrypt_token(session_token) if session_token else None

                # 创建 Team 记录
                team = Team(
                    email=email,
                    access_token_encrypted=encrypted_token,
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
                    last_sync=get_now()
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
        refresh_token: Optional[str] = None,
        session_token: Optional[str] = None,
        client_id: Optional[str] = None,
        email: Optional[str] = None,
        account_id: Optional[str] = None,
        max_members: Optional[int] = None,
        team_name: Optional[str] = None,
        status: Optional[str] = None
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

            # 3. 更新 Token
            if access_token:
                team.access_token_encrypted = encryption_service.encrypt_token(access_token)
            if refresh_token:
                team.refresh_token_encrypted = encryption_service.encrypt_token(refresh_token)
            if session_token:
                team.session_token_encrypted = encryption_service.encrypt_token(session_token)
            if client_id:
                team.client_id = client_id

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
                elif team.expires_at and team.expires_at < datetime.now():
                    team.status = "expired"
                else:
                    team.status = "active"

            await db_session.commit()


            logger.info(f"Team {team_id} 信息更新成功")
            return {"success": True, "message": "Team 信息更新成功"}

        except Exception as e:
            await db_session.rollback()
            logger.error(f"更新 Team 失败: {e}")
            return {"success": False, "error": f"更新失败: {str(e)}"}

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
            try:
                access_token = encryption_service.decrypt_token(team.access_token_encrypted)
            except Exception as e:
                logger.error(f"解密 Token 失败: {e}")

            return {
                "success": True,
                "team": {
                    "id": team.id,
                    "email": team.email,
                    "account_id": team.account_id,
                    "max_members": team.max_members,
                    "access_token": access_token,
                    "refresh_token": encryption_service.decrypt_token(team.refresh_token_encrypted) if team.refresh_token_encrypted else "",
                    "session_token": encryption_service.decrypt_token(team.session_token_encrypted) if team.session_token_encrypted else "",
                    "client_id": team.client_id or "",
                    "team_name": team.team_name,
                    "status": team.status,
                    "account_role": team.account_role
                }
            }
        except Exception as e:
            logger.error(f"获取 Team 信息失败: {e}")
            return {"success": False, "error": str(e)}

    async def import_team_batch(
        self,
        text: str,
        db_session: AsyncSession
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
                    refresh_token=data.get("refresh_token"),
                    session_token=data.get("session_token"),
                    client_id=data.get("client_id")
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
                return {
                    "success": False,
                    "message": None,
                    "error": f"Team ID {team_id} 不存在"
                }

            # 2. 确保 AT Token 有效
            access_token = await self.ensure_access_token(team, db_session, force_refresh=force_refresh)
            if not access_token:
                if team.status == "banned":
                    return {
                        "success": False,
                        "message": None,
                        "error": "Team 账号已封禁/失效 (token_invalidated)"
                    }
                return {
                    "success": False,
                    "message": None,
                    "error": "Token 已过期且无法刷新"
                }

            # 2.5 校验 Token 所属用户是否正确 (安全兜底)
            token_email = self.jwt_parser.extract_email(access_token)
            if token_email and team.email and token_email.lower() != team.email.lower():
                logger.error(f"Team {team_id} Token 邮箱不匹配: 预期 {team.email}, 实际 {token_email}")
                return {
                    "success": False,
                    "message": None,
                    "error": f"刷新出的账号身份 ({token_email}) 与原账号 ({team.email}) 不符，刷新已中止以防止数据污染。这可能是由于浏览器 Session 污染导致，建议清理 ST 后重新导入。"
                }

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
                            return {
                                "success": False,
                                "message": None,
                                "error": f"刷新出的账号身份 ({new_token_email}) 与原账号 ({team.email}) 不符。同步已中止。"
                            }

                        # 使用新 Token 再次尝试
                        account_result = await self.chatgpt_service.get_account_info(new_token, db_session, identifier=team.email)
                        if account_result["success"]:
                            logger.info(f"Team {team.id} 自动刷新 Token 后重试同步成功")
                            access_token = new_token
                        else:
                            # 刷新成功但请求依然失败，标记为过期/异常
                            logger.error(f"Team {team.id} Token 刷新成功但获取账户信息仍失败，标记为 expired")
                            team.status = "expired"
                            await db_session.commit()
                            return {
                                "success": False,
                                "message": None,
                                "error": f"Token 刷新成功但获取账户信息仍失败: {account_result.get('error', '未知错误')}"
                            }
                    else:
                        # 刷新失败，标记为过期
                        logger.error(f"Team {team.id} Token 刷新失败，标记为 expired")
                        team.status = "expired"
                        await db_session.commit()
                        return {
                            "success": False,
                            "message": None,
                            "error": "Token 已过期且通过 Session/Refresh Token 刷新失败"
                        }
                else:
                    # 其他非 Token 过期导致的错误
                    error_msg = account_result.get("error", "未知错误")
                    if account_result.get("error_code") == "account_deactivated":
                        error_msg = "账号已封禁 (account_deactivated)"
                    elif account_result.get("error_code") == "token_invalidated":
                        error_msg = "账号已封禁/失效 (token_invalidated)"
                    elif team.status == "error":
                        error_msg = "账号连续多次同步失败，已标记异常"
                        
                    return {
                        "success": False,
                        "message": None,
                        "error": error_msg
                    }

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

            # 5. 获取成员列表 (包含已加入和待加入)
            members_result = await self.chatgpt_service.get_members(
                access_token,
                current_account["account_id"],
                db_session,
                identifier=team.email
            )
            
            invites_result = await self.chatgpt_service.get_invites(
                access_token,
                current_account["account_id"],
                db_session,
                identifier=team.email
            )

            all_member_emails = set()
            current_members = 0
            if members_result["success"]:
                current_members += members_result["total"]
                for m in members_result.get("members", []):
                    if m.get("email"):
                        all_member_emails.add(m["email"].lower())
            
            if invites_result["success"]:
                current_members += invites_result["total"]
                for inv in invites_result.get("items", []):
                    if inv.get("email_address"):
                        all_member_emails.add(inv["email_address"].lower())
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
                    logger.error(f"Team {team.id} 获取成员列表连续失败 {team.error_count} 次，更新状态为 error")
                    team.status = "error"
                await db_session.commit()
                return {
                    "success": False,
                    "message": None,
                    "error": f"获取成员列表失败: {members_result['error']} (错误次数: {team.error_count})"
                }

            # 6. 解析过期时间
            expires_at = None
            if current_account["expires_at"]:
                try:
                    expires_at = datetime.fromisoformat(
                        current_account["expires_at"].replace("+00:00", "")
                    )
                except Exception as e:
                    logger.warning(f"解析过期时间失败: {e}")

            # 7. 确定状态
            status = "active"
            if current_members >= team.max_members:
                status = "full"
            elif expires_at and expires_at < datetime.now():
                status = "expired"
            
            # 8. 更新 Team 信息
            team.account_id = current_account["account_id"]
            team.team_name = current_account["name"]
            team.plan_type = current_account["plan_type"]
            team.subscription_plan = current_account["subscription_plan"]
            team.account_role = current_account.get("account_user_role")
            team.expires_at = expires_at
            team.current_members = current_members
            team.status = status
            team.error_count = 0  # 同步成功，重置错误次数
            team.last_sync = get_now()

            if not db_session.in_transaction():
                await db_session.commit()
            else:
                await db_session.flush()

            logger.info(f"Team 同步成功: ID {team_id}, 成员数 {current_members}")

            return {
                "success": True,
                "message": f"同步成功,当前成员数: {current_members}",
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
            
            # 处理已加入成员
            for m in members_result["members"]:
                all_members.append({
                    "user_id": m.get("id"),
                    "email": m.get("email"),
                    "name": m.get("name"),
                    "role": m.get("role"),
                    "added_at": m.get("created_time"),
                    "status": "joined"
                })
            
            # 处理待加入成员
            if invites_result["success"]:
                for inv in invites_result["items"]:
                    all_members.append({
                        "user_id": None, # 邀请还没有 user_id
                        "email": inv.get("email_address"),
                        "name": None,
                        "role": inv.get("role"),
                        "added_at": inv.get("created_time"),
                        "status": "invited"
                    })

            logger.info(f"获取 Team {team_id} 成员列表成功: 共 {len(all_members)} 个成员 (已加入: {members_result['total']})")

            # 6. 请求成功，重置错误状态
            await self._reset_error_status(team, db_session)

            return {
                "success": True,
                "members": all_members,
                "total": len(all_members),
                "error": None
            }

        except Exception as e:
            logger.error(f"获取成员列表失败: {e}")
            return {
                "success": False,
                "members": [],
                "total": 0,
                "error": f"获取成员列表失败: {str(e)}"
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
                return {
                    "success": False,
                    "message": None,
                    "error": f"Team ID {team_id} 不存在"
                }

            # 2. 调用 ChatGPT API 撤回邀请（请求级双保险）
            revoke_result = await self._request_with_refresh_retry(
                team,
                db_session,
                lambda token: self.chatgpt_service.delete_invite(
                    token,
                    team.account_id,
                    email,
                    db_session,
                    identifier=team.email
                )
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

        except Exception as e:
            await db_session.rollback()
            logger.error(f"撤回邀请失败: {e}")
            return {
                "success": False,
                "message": None,
                "error": f"撤回邀请失败: {str(e)}"
            }

    async def add_team_member(
        self,
        team_id: int,
        email: str,
        db_session: AsyncSession,
        is_legacy_customer: bool = False,
        legacy_remaining_warranty_days: Optional[int] = None
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
        try:
            # 1. 查询 Team
            stmt = select(Team).where(Team.id == team_id)
            result = await db_session.execute(stmt)
            team = result.scalar_one_or_none()

            if not team:
                return {
                    "success": False,
                    "message": None,
                    "error": f"Team ID {team_id} 不存在"
                }

            # 2. 检查 Team 状态
            if team.status == "full":
                return {
                    "success": False,
                    "message": None,
                    "error": "Team 已满,无法添加成员"
                }

            if team.status == "expired":
                return {
                    "success": False,
                    "message": None,
                    "error": "Team 已过期,无法添加成员"
                }

            # 3. 调用 ChatGPT API 发送邀请（请求级双保险：失败可触发刷新后重试）
            invite_result = await self._request_with_refresh_retry(
                team,
                db_session,
                lambda token: self.chatgpt_service.send_invite(
                    token,
                    team.account_id,
                    email,
                    db_session,
                    identifier=team.email
                )
            )

            if not invite_result["success"]:
                # 检查是否封号或 Token 失效
                if await self._handle_api_error(invite_result, team, db_session):
                    error_msg = invite_result.get("error", "未知错误")
                    if invite_result.get("error_code") == "account_deactivated":
                        error_msg = "账号已封禁 (account_deactivated)"
                    elif invite_result.get("error_code") == "token_invalidated":
                        error_msg = "Token 已失效 (token_invalidated)"
                        
                    return {
                        "success": False,
                        "message": None,
                        "error": error_msg
                    }

                return {
                    "success": False,
                    "message": None,
                    "error": f"发送邀请失败: {invite_result['error']}"
                }

            # 5. 更新成员数并二次校验邀请是否真的生效 (防止接口返回 200 但实际未加入)
            sync_res = await self.sync_team_info(team_id, db_session)
            member_emails = sync_res.get("member_emails", [])
            
            if email.lower() not in [m.lower() for m in member_emails]:
                logger.error(f"检测到“虚假成功”: Team {team_id} 发送邀请返回成功，但成员列表中未见该邮箱 {email}")
                # 标记错误
                await self._handle_api_error({"success": False, "error": "邀请发送成功但同步列表未见成员", "error_code": "ghost_success"}, team, db_session)
                return {
                    "success": False,
                    "message": None,
                    "error": "邀请发送成功但同步成员列表校验失败，该 Team 账号可能存在异常。"
                }

            await member_lifecycle_service.upsert_lifecycle_event(
                db_session,
                email=email,
                team_id=team_id,
                source_type="manual",
                event_type="legacy_seed" if is_legacy_customer else "manual_join",
                code_or_manual_tag="MANUAL",
                has_warranty=bool(is_legacy_customer),
                warranty_expires_at=(get_now() + timedelta(days=legacy_remaining_warranty_days)) if (is_legacy_customer and legacy_remaining_warranty_days is not None) else None,
                is_legacy_seeded=is_legacy_customer,
                legacy_remaining_warranty_days=legacy_remaining_warranty_days,
            )

            await db_session.commit()

            logger.info(f"添加成员成功: {email} -> Team {team_id}")

            # 6. 请求成功，重置错误状态
            await self._reset_error_status(team, db_session)

            return {
                "success": True,
                "message": f"邀请已发送到 {email}",
                "error": None
            }

        except Exception as e:
            await db_session.rollback()
            logger.error(f"添加成员失败: {e}")
            return {
                "success": False,
                "message": None,
                "error": f"添加成员失败: {str(e)}"
            }

    async def delete_team_member(
        self,
        team_id: int,
        user_id: str,
        db_session: AsyncSession
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
                return {
                    "success": False,
                    "message": None,
                    "error": f"Team ID {team_id} 不存在"
                }

            # 2. 调用 ChatGPT API 删除成员（请求级双保险）
            delete_result = await self._request_with_refresh_retry(
                team,
                db_session,
                lambda token: self.chatgpt_service.delete_member(
                    token,
                    team.account_id,
                    user_id,
                    db_session,
                    identifier=team.email
                )
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

        except Exception as e:
            await db_session.rollback()
            logger.error(f"删除成员失败: {e}")
            return {
                "success": False,
                "message": None,
                "error": f"删除成员失败: {str(e)}"
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

            logger.info(f"Team {team_id} ({team.email}) 开启设备身份验证成功")
            return {"success": True, "message": "设备代码身份验证开启成功"}

        except Exception as e:
            logger.error(f"开启设备身份验证失败: {e}")
            return {"success": False, "error": f"异常: {str(e)}"}

    async def get_available_teams(
        self,
        db_session: AsyncSession
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
                Team.current_members < Team.max_members
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

        except Exception as e:
            logger.error(f"获取可用 Team 列表失败: {e}")
            return {
                "success": False,
                "teams": [],
                "error": f"获取列表失败: {str(e)}"
            }

    async def get_total_available_spots(
        self,
        db_session: AsyncSession
    ) -> int:
        """
        获取剩余车位总数

        Args:
            db_session: 数据库会话

        Returns:
            剩余车位总数
        """
        try:
            # 计算所有 active Team 的剩余车位总和
            # remaining = max_members - current_members
            stmt = select(
                func.sum(Team.max_members - Team.current_members)
            ).where(
                Team.status == "active",
                Team.current_members < Team.max_members
            )
            
            result = await db_session.execute(stmt)
            total_spots = result.scalar() or 0
            
            return int(total_spots)

        except Exception as e:
            logger.error(f"获取剩余车位失败: {e}")
            return 0



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
            refresh_token = ""
            session_token = ""
            try:
                if team.access_token_encrypted:
                    access_token = encryption_service.decrypt_token(team.access_token_encrypted)
                if team.refresh_token_encrypted:
                    refresh_token = encryption_service.decrypt_token(team.refresh_token_encrypted)
                if team.session_token_encrypted:
                    session_token = encryption_service.decrypt_token(team.session_token_encrypted)
            except Exception as e:
                logger.error(f"解密 Team {team_id} Token 失败: {e}")

            # 构建返回数据
            team_data = {
                "id": team.id,
                "email": team.email,
                "account_id": team.account_id,
                "access_token": access_token,
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

        except Exception as e:
            logger.error(f"获取 Team 详情失败: {e}")
            return {
                "success": False,
                "team": None,
                "team_accounts": [],
                "error": f"获取 Team 详情失败: {str(e)}"
            }

    async def get_all_teams(
        self,
        db_session: AsyncSession,
        page: int = 1,
        per_page: int = 20,
        search: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        获取所有 Team 列表 (用于管理员页面)

        Args:
            db_session: 数据库会话
            page: 页码
            per_page: 每页数量
            search: 搜索关键词

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

            # 3. 获取总数
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
                    "last_sync": team.last_sync.isoformat() if team.last_sync else None,
                    "created_at": team.created_at.isoformat() if team.created_at else None
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
                # 即使没找到也返回成功，以便上层逻辑继续更新记录
                return {"success": True, "message": "成员已不存在"}

            # 3. 根据状态执行删除
            if target["status"] == "joined":
                # 已加入，调用删除成员
                return await self.delete_team_member(team_id, target["user_id"], db_session)
            else:
                # 待加入，调用撤回邀请
                return await self.revoke_team_invite(team_id, email, db_session)

        except Exception as e:
            logger.error(f"撤回邀请或删除成员时发生异常: {e}")
            return {"success": False, "error": str(e)}


    async def cleanup_expired_teams(
        self,
        db_session: AsyncSession,
        retention_days: int = 30
    ) -> Dict[str, Any]:
        """
        自动清理到期超过 retention_days 天的 Team。

        规则：
        - 若 Team 关联了仍在质保期内的兑换码，则跳过（保护后续质保复用）。
        - 无质保保护时：有兑换记录则软删除；无兑换记录则硬删除。
        """
        try:
            now = get_now()
            cutoff = now - timedelta(days=max(1, retention_days))

            stmt = select(Team).where(
                Team.expires_at.is_not(None),
                Team.expires_at < cutoff
            )
            result = await db_session.execute(stmt)
            expired_teams = result.scalars().all()

            hard_deleted = 0
            soft_deleted = 0
            skipped_warranty = 0

            for team in expired_teams:
                # 已软删除的不重复处理
                if team.status == "deleted":
                    continue

                # 若存在仍在质保期内的兑换码，跳过自动删除
                active_warranty_stmt = (
                    select(func.count(RedemptionCode.id))
                    .join(RedemptionRecord, RedemptionRecord.code == RedemptionCode.code)
                    .where(
                        RedemptionRecord.team_id == team.id,
                        RedemptionCode.has_warranty.is_(True),
                        RedemptionCode.warranty_expires_at.is_not(None),
                        RedemptionCode.warranty_expires_at > now
                    )
                )
                active_warranty_result = await db_session.execute(active_warranty_stmt)
                active_warranty_count = active_warranty_result.scalar() or 0
                if active_warranty_count > 0:
                    skipped_warranty += 1
                    continue

                records_count_stmt = select(func.count(RedemptionRecord.id)).where(RedemptionRecord.team_id == team.id)
                records_count_result = await db_session.execute(records_count_stmt)
                records_count = records_count_result.scalar() or 0

                if records_count > 0:
                    team.status = "deleted"
                    team.team_name = f"[自动删除] {team.team_name or team.email}"
                    team.last_sync = now
                    await db_session.execute(
                        delete(TeamAccount).where(TeamAccount.team_id == team.id)
                    )
                    soft_deleted += 1
                else:
                    await db_session.delete(team)
                    hard_deleted += 1

            await db_session.commit()

            return {
                "success": True,
                "hard_deleted": hard_deleted,
                "soft_deleted": soft_deleted,
                "skipped_warranty": skipped_warranty,
                "total_candidates": len(expired_teams),
                "error": None
            }
        except Exception as e:
            await db_session.rollback()
            logger.error(f"自动清理 Team 失败: {e}")
            return {
                "success": False,
                "hard_deleted": 0,
                "soft_deleted": 0,
                "skipped_warranty": 0,
                "total_candidates": 0,
                "error": f"自动清理 Team 失败: {str(e)}"
            }

    async def delete_team(
        self,
        team_id: int,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        删除 Team

        对于存在兑换记录的 Team，采用“软删除”以保留质保/历史关联；
        对于无兑换记录的 Team，执行“硬删除”。

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
                return {
                    "success": False,
                    "message": None,
                    "error": f"Team ID {team_id} 不存在"
                }

            # 2. 判断是否存在兑换记录（含质保关联）
            records_count_stmt = select(func.count(RedemptionRecord.id)).where(RedemptionRecord.team_id == team_id)
            records_count_result = await db_session.execute(records_count_stmt)
            records_count = records_count_result.scalar() or 0

            if records_count > 0:
                # 2.1 软删除：保留 Team 与兑换记录关联，避免影响质保查询
                team.status = "deleted"
                team.team_name = f"[已删除] {team.team_name or team.email}"
                team.last_sync = get_now()

                # 清理 TeamAccount，避免后续被当作可用 Team 使用
                await db_session.execute(
                    delete(TeamAccount).where(TeamAccount.team_id == team_id)
                )

                await db_session.commit()
                logger.info(f"Team {team_id} 存在 {records_count} 条兑换记录，已执行软删除")
                return {
                    "success": True,
                    "message": "Team 已软删除（保留质保与历史记录）",
                    "error": None
                }

            # 2.2 无兑换记录时执行硬删除
            await db_session.execute(
                update(RedemptionCode)
                .where(RedemptionCode.used_team_id == team_id)
                .values(used_team_id=None)
            )

            await db_session.delete(team)
            await db_session.commit()

            logger.info(f"删除 Team {team_id} 成功")

            return {
                "success": True,
                "message": "Team 已删除",
                "error": None
            }

        except Exception as e:
            await db_session.rollback()
            logger.error(f"删除 Team 失败: {e}")
            return {
                "success": False,
                "message": None,
                "error": f"删除 Team 失败: {str(e)}"
            }

    async def get_total_available_seats(
        self,
        db_session: AsyncSession
    ) -> int:
        """
        获取所有活跃 Team 的总剩余车位数
        """
        try:
            # 统计所有状态为 active 的 Team 的剩余位置
            stmt = select(func.sum(Team.max_members - Team.current_members)).where(Team.status == "active")
            result = await db_session.execute(stmt)
            return result.scalar() or 0
        except Exception as e:
            logger.error(f"获取总可用车位数失败: {e}")
            return 0

    async def get_stats(
        self,
        db_session: AsyncSession
    ) -> Dict[str, int]:
        """获取 Team 统计信息"""
        try:
            # 总数
            total_stmt = select(func.count(Team.id))
            total_result = await db_session.execute(total_stmt)
            total = total_result.scalar() or 0
            
            # 可用 Team 数 (状态为 active 且未满)
            available_stmt = select(func.count(Team.id)).where(
                Team.status == "active",
                Team.current_members < Team.max_members
            )
            available_result = await db_session.execute(available_stmt)
            available = available_result.scalar() or 0
            
            return {
                "total": total,
                "available": available
            }
        except Exception as e:
            logger.error(f"获取 Team 统计信息失败: {e}")
            return {"total": 0, "available": 0}


# 创建全局 Team 服务实例
team_service = TeamService()
