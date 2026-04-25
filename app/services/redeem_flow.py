"""
兑换流程服务 (Redeem Flow Service)
协调兑换码验证, Team 选择和加入 Team 的完整流程
"""
import logging
import asyncio
import traceback
from collections import defaultdict
from typing import Optional, Dict, Any, List, Set
from datetime import datetime, timedelta
from sqlalchemy import select, update, delete, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import AsyncSessionLocal

from app.models import RedemptionCode, RedemptionRecord, Team, Setting
from app.services.redemption import RedemptionService
from app.services.team import TeamService
from app.services.warranty import warranty_service
from app.services.notification import notification_service
from app.services.settings import (
    settings_service,
    WARRANTY_EXPIRATION_MODE_REFRESH_ON_REDEEM,
)
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)

ALREADY_IN_TEAM_KEYWORDS = ["already in workspace", "already in team", "already a member"]

# 全局兑换锁: 针对 code 进行加锁，防止同一个码并发进入兑换
# 使用引用计数管理，避免 defaultdict 永久持有每个见过的 key
_code_locks: Dict[Any, asyncio.Lock] = {}
_code_lock_refs: Dict[Any, int] = {}
_code_locks_guard = asyncio.Lock()

# 全局 Team 锁: 针对 Team 进行加锁，防止并发拉人导致的人数状态不同步
_team_locks: Dict[Any, asyncio.Lock] = {}
_team_lock_refs: Dict[Any, int] = {}
_team_locks_guard = asyncio.Lock()


class _KeyedLockManager:
    """按 key 分配 asyncio.Lock，使用引用计数在无使用者时自动释放。"""

    def __init__(self, locks: Dict[Any, asyncio.Lock], refs: Dict[Any, int], guard: asyncio.Lock):
        self._locks = locks
        self._refs = refs
        self._guard = guard

    def acquire(self, key: Any):
        return _KeyedLockContext(self, key)


class _KeyedLockContext:
    def __init__(self, manager: _KeyedLockManager, key: Any):
        self._manager = manager
        self._key = key
        self._lock: Optional[asyncio.Lock] = None

    async def __aenter__(self):
        async with self._manager._guard:
            lock = self._manager._locks.get(self._key)
            if lock is None:
                lock = asyncio.Lock()
                self._manager._locks[self._key] = lock
            self._manager._refs[self._key] = self._manager._refs.get(self._key, 0) + 1
            self._lock = lock
        await self._lock.acquire()
        return self._lock

    async def __aexit__(self, exc_type, exc, tb):
        try:
            self._lock.release()
        finally:
            async with self._manager._guard:
                new_refs = self._manager._refs.get(self._key, 1) - 1
                if new_refs <= 0:
                    self._manager._refs.pop(self._key, None)
                    # 仅当没有其他等待者时才从表中移除，避免删掉别人等待的锁对象
                    current_lock = self._manager._locks.get(self._key)
                    if current_lock is self._lock and not self._lock.locked():
                        self._manager._locks.pop(self._key, None)
                else:
                    self._manager._refs[self._key] = new_refs


_code_lock_manager = _KeyedLockManager(_code_locks, _code_lock_refs, _code_locks_guard)
_team_lock_manager = _KeyedLockManager(_team_locks, _team_lock_refs, _team_locks_guard)


class RedeemFlowService:
    """兑换流程场景服务类"""

    def __init__(self):
        """初始化兑换流程服务"""
        from app.services.chatgpt import chatgpt_service
        self.redemption_service = RedemptionService()
        self.warranty_service = warranty_service
        self.team_service = TeamService()
        self.chatgpt_service = chatgpt_service

    async def verify_code_and_get_teams(
        self,
        code: str,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        验证兑换码并返回可用 Team 列表
        针对 aiosqlite 进行优化，避免 greenlet_spawn 报错
        """
        try:
            # 1. 验证兑换码
            validate_result = await self.redemption_service.validate_code(code, db_session)

            if not validate_result["success"]:
                return {
                    "success": False,
                    "valid": False,
                    "reason": None,
                    "teams": [],
                    "error": validate_result["error"]
                }

            # 如果是已经标记为过期了
            if not validate_result["valid"] and validate_result.get("reason") == "兑换码已过期 (超过首次兑换截止时间)":
                try:
                    await db_session.commit()
                except Exception:
                    pass

            if not validate_result["valid"]:
                return {
                    "success": True,
                    "valid": False,
                    "reason": validate_result["reason"],
                    "teams": [],
                    "error": None
                }

            # 2. 获取可用 Team 列表
            code_meta = validate_result.get("redemption_code") or {}
            pool_type = code_meta.get("pool_type", "normal")
            teams: List[Dict[str, Any]] = []
            if code_meta.get("virtual_welfare_code"):
                bound_team_id = int(code_meta.get("team_id") or 0)
                if bound_team_id <= 0:
                    return {
                        "success": True,
                        "valid": False,
                        "reason": "福利通用兑换码未绑定有效 Team，请联系管理员重新生成",
                        "teams": [],
                        "error": None
                    }

                team_result = await self.team_service.get_team_by_id(bound_team_id, db_session)
                if not team_result.get("success"):
                    return {
                        "success": True,
                        "valid": False,
                        "reason": "福利通用兑换码绑定的 Team 不存在，请联系管理员重新生成",
                        "teams": [],
                        "error": None
                    }

                bound_team = team_result.get("team") or {}
                if (
                    bound_team.get("status") != "active"
                    or int(bound_team.get("current_members") or 0) >= int(bound_team.get("max_members") or 0)
                ):
                    return {
                        "success": True,
                        "valid": False,
                        "reason": "福利通用兑换码绑定的 Team 当前不可用，请联系管理员重新生成",
                        "teams": [],
                        "error": None
                    }

                teams = [{
                    "id": bound_team["id"],
                    "team_name": bound_team.get("team_name"),
                    "current_members": bound_team.get("current_members"),
                    "max_members": bound_team.get("max_members"),
                    "expires_at": bound_team.get("expires_at"),
                    "subscription_plan": bound_team.get("subscription_plan"),
                }]
            else:
                teams_result = await self.team_service.get_available_teams(db_session, pool_type=pool_type)

                if not teams_result["success"]:
                    return {
                        "success": False,
                        "valid": True,
                        "reason": "兑换码有效",
                        "teams": [],
                        "error": teams_result["error"]
                    }
                teams = teams_result["teams"]

            logger.info(f"验证兑换码成功: {code}, 可用 Team 数量: {len(teams)}")

            return {
                "success": True,
                "valid": True,
                "reason": "兑换码有效",
                "teams": teams,
                "error": None
            }

        except Exception as e:
            logger.error(f"验证兑换码并获取 Team 列表失败: {e}")
            logger.error(traceback.format_exc())
            return {
                "success": False,
                "valid": False,
                "reason": None,
                "teams": [],
                "error": f"验证失败: {str(e)}"
            }

    async def select_team_auto(
        self,
        db_session: AsyncSession,
        email: Optional[str] = None,
        exclude_team_ids: Optional[List[int]] = None,
        pool_type: str = "normal"
    ) -> Dict[str, Any]:
        """
        自动选择一个可用的 Team。
        如果提供邮箱，优先使用本地 Team-邮箱映射表排除该邮箱已占用的 Team。
        """
        try:
            excluded_ids = set(exclude_team_ids or [])
            mapped_team_ids = set()
            if email:
                mapped_team_ids = set(
                    await self.team_service.get_active_team_ids_for_email(
                        email,
                        db_session,
                        pool_type=pool_type
                    )
                )
                excluded_ids.update(mapped_team_ids)

            # 查找所有 active 且未满的 Team
            stmt = select(Team).where(
                Team.status == "active",
                Team.current_members < Team.max_members,
                Team.pool_type == pool_type
            )

            if excluded_ids:
                stmt = stmt.where(Team.id.not_in(sorted(excluded_ids)))

            # 按 Team ID 从小到大顺序分配：当前 Team 满员后再轮到下一个 Team
            stmt = stmt.order_by(Team.id.asc())

            result = await db_session.execute(stmt)
            team = result.scalars().first()

            if not team:
                reason = "没有可用的 Team"
                if mapped_team_ids:
                    reason = "没有新的可用 Team；您已加入所有当前有席位的 Team，当前兑换码不会被消耗"
                elif exclude_team_ids:
                    reason = "您已加入所有可用 Team"
                return {
                    "success": False,
                    "team_id": None,
                    "error": reason
                }

            logger.info(f"自动选择 Team: {team.id}")
            return {
                "success": True,
                "team_id": team.id,
                "error": None
            }

        except Exception as e:
            logger.error(f"自动选择 Team 失败: {e}")
            return {
                "success": False,
                "team_id": None,
                "error": f"自动选择 Team 失败: {str(e)}"
            }

    async def redeem_and_join_team(
        self,
        email: str,
        code: str,
        team_id: Optional[int],
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        完整的兑换流程 (带事务和并发控制)
        """
        last_error = "未知错误"
        max_retries = 5
        current_target_team_id = team_id
        core_success = False
        success_result = None
        team_id_final = None
        selected_team_locked = team_id is not None
        excluded_team_ids: Set[int] = set()

        # 针对 code 加锁，防止同一个码并发进入兑换
        async with _code_lock_manager.acquire(code):
            for attempt in range(max_retries):
                logger.info(f"兑换尝试 {attempt + 1}/{max_retries} (Code: {code}, Email: {email})")
                seat_reserved = False

                try:
                    validate_result = await self.redemption_service.validate_code(code, db_session)
                    if not validate_result.get("success"):
                        return {"success": False, "error": validate_result.get("error") or "兑换码校验失败"}
                    if not validate_result.get("valid"):
                        return {"success": False, "error": validate_result.get("reason") or "兑换码无效"}
                    code_meta = validate_result.get("redemption_code") or {}
                    pool_type = code_meta.get("pool_type", "normal")
                    is_virtual_welfare_code = bool(code_meta.get("virtual_welfare_code"))
                    bound_welfare_team_id = int(code_meta.get("team_id") or 0) if is_virtual_welfare_code else 0

                    if is_virtual_welfare_code and bound_welfare_team_id <= 0:
                        return {"success": False, "error": "福利通用兑换码未绑定有效 Team，请联系管理员重新生成"}

                    if selected_team_locked:
                        existing_team_ids = set(
                            await self.team_service.get_active_team_ids_for_email(
                                email,
                                db_session,
                                pool_type=pool_type
                            )
                        )
                        if team_id in existing_team_ids:
                            return {
                                "success": False,
                                "error": (
                                    f"您已在 Team {team_id} 中，当前兑换码不会被消耗。"
                                    "如需新的工作空间，请选择其他 Team。"
                                )
                            }

                    # 确定目标 Team (初选)
                    if is_virtual_welfare_code:
                        if selected_team_locked and current_target_team_id != bound_welfare_team_id:
                            return {"success": False, "error": "当前福利通用兑换码仅可兑换到其绑定的 Team"}
                        team_id_final = bound_welfare_team_id
                        current_target_team_id = bound_welfare_team_id
                    else:
                        team_id_final = current_target_team_id
                        if not team_id_final:
                            select_res = await self.select_team_auto(
                                db_session,
                                email=email,
                                exclude_team_ids=sorted(excluded_team_ids) if excluded_team_ids else None,
                                pool_type=pool_type
                            )
                            if not select_res["success"]:
                                return {"success": False, "error": select_res["error"]}
                            team_id_final = select_res["team_id"]
                            current_target_team_id = team_id_final

                    # 使用 Team 锁序列化对该账户的操作，防止并发冲突
                    async with _team_lock_manager.acquire(team_id_final):
                        logger.info(f"锁定 Team {team_id_final} 执行核心兑换步骤 (尝试 {attempt+1})")

                        # 重置 Session 状态，确保没有残留事务（应对上一轮迭代可能的失败）
                        if db_session.in_transaction():
                            await db_session.rollback()
                        elif db_session.is_active:
                            await db_session.rollback()

                        # 2. 核心校验 (开启短事务)
                        if not db_session.in_transaction():
                            await db_session.begin()

                        try:
                            # 1. 验证和锁定码（福利通用码不在 redemption_codes 表落库）
                            rc = None
                            if not is_virtual_welfare_code:
                                stmt = select(RedemptionCode).where(RedemptionCode.code == code).with_for_update()
                                res = await db_session.execute(stmt)
                                rc = res.scalar_one_or_none()

                                if not rc:
                                    await db_session.rollback()
                                    return {"success": False, "error": "兑换码不存在"}

                                if not rc.reusable_by_seat:
                                    if rc.status not in ["unused", "warranty_active"]:
                                        if rc.status == "used":
                                            warranty_check = await self.warranty_service.validate_warranty_reuse(
                                                db_session, code, email
                                            )
                                            if not warranty_check.get("can_reuse"):
                                                await db_session.rollback()
                                                return {"success": False, "error": warranty_check.get("reason") or "兑换码已使用"}
                                        else:
                                            await db_session.rollback()
                                            return {"success": False, "error": f"兑换码状态无效: {rc.status}"}

                            # 2. 以数据库原子更新的方式预留席位，避免多实例并发时超拉
                            reserve_result = await self.team_service.reserve_seat_if_available(
                                team_id=team_id_final,
                                db_session=db_session,
                                pool_type=pool_type,
                            )
                            if not reserve_result["success"]:
                                raise Exception(reserve_result["error"])
                            target_team = reserve_result["team"]
                            seat_reserved = True

                            # 提取必要信息后立即提交，释放 DB 锁以进行耗时的 API 调用
                            account_id_to_use = target_team.account_id
                            team_email_to_use = target_team.email
                            await db_session.commit()
                        except Exception as e:
                            if db_session.in_transaction():
                                await db_session.rollback()
                            raise e

                        # 3. 执行 API 邀请 (耗时操作，放事务外)
                        # 必须重新加载 target_team
                        res = await db_session.execute(select(Team).where(Team.id == team_id_final))
                        target_team = res.scalar_one_or_none()

                        access_token = await self.team_service.ensure_access_token(target_team, db_session)
                        if not access_token:
                            raise Exception("获取 Team 访问权限失败，账户状态异常")

                        invite_res = await self.chatgpt_service.send_invite(
                            access_token, account_id_to_use, email, db_session,
                            identifier=team_email_to_use
                        )

                        # 4. 后置处理与状态持久化 (第二次短事务)
                        if not db_session.in_transaction():
                            await db_session.begin()

                        try:
                            # 重新载入，确保状态最新
                            rc = None
                            if not is_virtual_welfare_code:
                                res = await db_session.execute(
                                    select(RedemptionCode).where(RedemptionCode.code == code).with_for_update()
                                )
                                rc = res.scalar_one_or_none()
                            res = await db_session.execute(
                                select(Team).where(Team.id == team_id_final, Team.pool_type == pool_type).with_for_update()
                            )
                            target_team = res.scalar_one_or_none()

                            if not invite_res["success"]:
                                err = invite_res.get("error", "邀请失败")
                                err_str = str(err).lower()

                                if any(kw in err_str for kw in ALREADY_IN_TEAM_KEYWORDS):
                                    logger.info(f"用户 {email} 已经在 Team {team_id_final} 中，本次兑不消耗兑换码")

                                    if seat_reserved and target_team.current_members > 0:
                                        target_team.current_members -= 1
                                        seat_reserved = False

                                    if target_team.current_members >= target_team.max_members:
                                        target_team.status = "full"
                                    elif target_team.expires_at and target_team.expires_at < get_now():
                                        target_team.status = "expired"
                                    else:
                                        target_team.status = "active"

                                    await self.team_service.upsert_team_email_mapping(
                                        team_id_final,
                                        email,
                                        status="joined",
                                        db_session=db_session,
                                        source="api"
                                    )
                                    await db_session.commit()

                                    message = (
                                        f"您已在 Team {team_id_final} 中，当前兑换码不会被消耗。"
                                        "如需新的工作空间，请选择其他 Team。"
                                    )

                                    if selected_team_locked:
                                        return {"success": False, "error": message}

                                    excluded_team_ids.add(team_id_final)
                                    current_target_team_id = None
                                    raise Exception(
                                        f"该邮箱已在 Team {team_id_final} 中，自动切换其他 Team"
                                    )

                                else:
                                    if any(kw in err_str for kw in ["maximum number of seats", "full", "no seats"]):
                                        if seat_reserved:
                                            target_team.current_members = max(
                                                target_team.current_members - 1,
                                                0
                                            )
                                            seat_reserved = False
                                        if target_team.current_members >= target_team.max_members:
                                            target_team.status = "full"
                                        elif target_team.expires_at and target_team.expires_at < get_now():
                                            target_team.status = "expired"
                                        else:
                                            target_team.status = "active"
                                        await db_session.commit()
                                        raise Exception(f"该 Team 席位已满 (API Error: {err})")

                                    if seat_reserved and target_team.current_members > 0:
                                        target_team.current_members -= 1
                                        seat_reserved = False

                                    if target_team.current_members >= target_team.max_members:
                                        target_team.status = "full"
                                    elif target_team.expires_at and target_team.expires_at < get_now():
                                        target_team.status = "expired"
                                    else:
                                        target_team.status = "active"

                                    await self.team_service._handle_api_error(
                                        {
                                            "success": False,
                                            "error": err,
                                            "error_code": invite_res.get("error_code"),
                                        },
                                        target_team,
                                        db_session,
                                    )

                                    if db_session.in_transaction():
                                        await db_session.commit()

                                    if target_team.status in {"banned", "expired", "error"}:
                                        unavailable_msg = (
                                            f"Team {team_id_final} 登录状态已失效({target_team.status})，"
                                            "请刷新后重试或选择其他 Team"
                                        )
                                        if selected_team_locked:
                                            return {"success": False, "error": unavailable_msg}

                                        excluded_team_ids.add(team_id_final)
                                        current_target_team_id = None
                                        raise Exception(
                                            f"Team {team_id_final} 状态已更新为 {target_team.status}，自动切换其他 Team"
                                        )

                                    raise Exception(err)

                            invite_data = invite_res.get("data", {})
                            if "account_invites" in invite_data and not invite_data.get("account_invites"):
                                if seat_reserved and target_team.current_members > 0:
                                    target_team.current_members -= 1
                                    seat_reserved = False
                                if target_team.current_members >= target_team.max_members:
                                    target_team.status = "full"
                                elif target_team.expires_at and target_team.expires_at < get_now():
                                    target_team.status = "expired"
                                else:
                                    target_team.status = "active"
                                await db_session.commit()
                                raise Exception("Team账号受限: 官方拦截下发(响应空列表)，请检查账单/风控状态")

                            # 成功逻辑
                            if rc and (not rc.reusable_by_seat):
                                warranty_expiration_mode = None
                                if rc.has_warranty:
                                    warranty_expiration_mode = await settings_service.get_warranty_expiration_mode(db_session)

                                previous_used_at = rc.used_at
                                current_use_time = get_now()
                                rc.status = "used"
                                rc.used_by_email = email
                                rc.used_team_id = team_id_final
                                should_refresh_warranty_window = bool(
                                    rc.has_warranty and (
                                        previous_used_at is None
                                        or warranty_expiration_mode == WARRANTY_EXPIRATION_MODE_REFRESH_ON_REDEEM
                                    )
                                )

                                if (not rc.has_warranty) or should_refresh_warranty_window:
                                    rc.used_at = current_use_time
                                if rc.has_warranty:
                                    extension_days = max(int(getattr(rc, "extension_days", 0) or 0), 0)
                                    days = (rc.warranty_days or 30) + extension_days
                                    if should_refresh_warranty_window:
                                        rc.warranty_expires_at = current_use_time + timedelta(days=days)
                                    elif not rc.warranty_expires_at:
                                        base_time = previous_used_at or current_use_time
                                        first_use_result = await db_session.execute(
                                            select(func.min(RedemptionRecord.redeemed_at))
                                            .where(RedemptionRecord.code == code)
                                        )
                                        first_use_time = first_use_result.scalar()
                                        if first_use_time:
                                            base_time = first_use_time
                                        rc.warranty_expires_at = base_time + timedelta(days=days)

                            if is_virtual_welfare_code:
                                current_welfare_code = (await settings_service.get_setting(
                                    db_session,
                                    "welfare_common_code",
                                    "",
                                    use_cache=False,
                                ) or "").strip()
                                if not current_welfare_code or current_welfare_code != code:
                                    raise Exception("兑换码已失效，请刷新页面后使用最新福利通用兑换码")

                                bound_team_id_raw = await settings_service.get_setting(
                                    db_session,
                                    "welfare_common_code_team_id",
                                    "0",
                                    use_cache=False,
                                )
                                try:
                                    current_bound_team_id = int(str(bound_team_id_raw or "0").strip() or 0)
                                except Exception:
                                    current_bound_team_id = 0
                                if current_bound_team_id <= 0 or current_bound_team_id != team_id_final:
                                    raise Exception("当前福利通用兑换码仅可兑换到其绑定的 Team")

                                configured_limit_raw = await settings_service.get_setting(
                                    db_session,
                                    "welfare_common_code_limit",
                                    "0",
                                    use_cache=False,
                                )
                                try:
                                    configured_limit = int(str(configured_limit_raw or "0").strip() or 0)
                                except Exception:
                                    configured_limit = 0

                                setting_res = await db_session.execute(
                                    select(Setting)
                                    .where(Setting.key == "welfare_common_code_used_count")
                                    .with_for_update()
                                )
                                used_setting = setting_res.scalar_one_or_none()

                                if used_setting:
                                    try:
                                        current_used = int((used_setting.value or "0").strip() or 0)
                                    except Exception:
                                        current_used = 0
                                else:
                                    current_used = 0
                                    used_setting = Setting(
                                        key="welfare_common_code_used_count",
                                        value="0",
                                        description="福利通用兑换码已使用次数"
                                    )
                                    db_session.add(used_setting)

                                if configured_limit > 0 and current_used >= configured_limit:
                                    raise Exception("兑换码次数已用完，无法进行兑换")

                                used_setting.value = str(current_used + 1)
                                # 注意：缓存写入必须等到事务成功 commit 之后再做，
                                # 否则一旦后续步骤失败 rollback，进程内缓存会被永久污染。

                            if is_virtual_welfare_code:
                                await self.redemption_service.ensure_virtual_welfare_shadow_code(db_session, code)

                            record = RedemptionRecord(
                                email=email,
                                code=code,
                                team_id=team_id_final,
                                account_id=target_team.account_id,
                                is_warranty_redemption=(bool(rc and rc.has_warranty and (not rc.reusable_by_seat)))
                            )
                            db_session.add(record)

                            await self.team_service.upsert_team_email_mapping(
                                team_id_final,
                                email,
                                status="invited",
                                db_session=db_session,
                                source="redeem"
                            )

                            seat_reserved = False

                            await db_session.commit()

                            # 事务提交成功后再刷新进程内缓存，防止回滚后缓存与 DB 脱节。
                            if is_virtual_welfare_code and used_setting is not None:
                                settings_service._cache["welfare_common_code_used_count"] = used_setting.value

                            # 核心步骤成功，准备返回结果
                            success_result = {
                                "success": True,
                                "message": "兑换成功！邀请链接已发送至您的邮箱，请及时查收。",
                                "team_info": {
                                    "id": team_id_final,
                                    "team_name": target_team.team_name,
                                    "email": target_team.email,
                                    "expires_at": target_team.expires_at.isoformat() if target_team.expires_at else None
                                }
                            }
                            core_success = True
                        except Exception as e:
                            if db_session.in_transaction():
                                await db_session.rollback()
                            raise e

                    # 如果核心步骤成功，跳出重试循环
                    if core_success:
                        break

                except Exception as e:
                    last_error = str(e)
                    logger.error(f"兑换迭代失败 ({attempt+1}): {last_error}")

                    try:
                        if db_session.in_transaction():
                            await db_session.rollback()
                    except Exception:
                        pass

                    if seat_reserved and team_id_final:
                        try:
                            if db_session.in_transaction():
                                await db_session.rollback()
                            await db_session.begin()
                            reserved_team = await db_session.get(Team, team_id_final)
                            if reserved_team:
                                await self.team_service.release_reserved_seat(
                                    team_id=team_id_final,
                                    db_session=db_session,
                                    pool_type=reserved_team.pool_type or "normal",
                                )
                                await db_session.commit()
                            else:
                                await db_session.rollback()
                        except Exception:
                            if db_session.in_transaction():
                                await db_session.rollback()

                    # 判读是否中断重试
                    if any(kw in last_error for kw in ["不存在", "已使用", "已有正在使用", "质保已过期", "当前兑换码不会被消耗"]):
                        return {"success": False, "error": last_error}

                    # 判定是否需要永久标记为“满员”
                    if any(kw in last_error.lower() for kw in ["已满", "seats", "full"]):
                        try:
                            if not team_id:
                                from sqlalchemy import update as sqlalchemy_update
                                await db_session.execute(
                                    sqlalchemy_update(Team).where(Team.id == team_id_final).values(status="full")
                                )
                                await db_session.commit()
                            current_target_team_id = None
                        except Exception:
                            pass

                    if attempt < max_retries - 1:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue

            if core_success:
                # 后台异步验证任务 (循环检测 3 次，确保 API 数据同步) - 移至后台以极大提高并发并防止 HTTP 超时
                asyncio.create_task(self._background_verify_sync(team_id_final, email))

                # 补货通知任务 (异步)
                asyncio.create_task(notification_service.check_and_notify_low_stock())

                return success_result
            else:
                return {
                    "success": False,
                    "error": f"兑换失败次数过多。最后报错: {last_error}"
                }

    async def _background_verify_sync(self, team_id: int, email: str):
        """
        后台异步验证并同步 (不阻塞 HTTP 请求)
        """
        async with AsyncSessionLocal() as db_session:
            try:
                is_verified = False
                for i in range(3):
                    await asyncio.sleep(5)
                    # 每次同步前确保 session 是最新的
                    sync_res = await self.team_service.sync_team_info(team_id, db_session)
                    member_emails = [m.lower() for m in sync_res.get("member_emails", [])]
                    if email.lower() in member_emails:
                        is_verified = True
                        logger.info(f"Team {team_id} [Background] 同步确认成功 (尝试第 {i+1} 次)")
                        break

                    if i < 2:
                        logger.warning(f"Team {team_id} [Background] 尚未见到成员 {email}，准备第 {i+2} 次重试...")

                if not is_verified:
                    logger.error(f"检测到“虚假成功”: Team {team_id} 兑换成功但 15s 后仍查不到成员 {email}")
                    # 在后台标记异常
                    stmt = select(Team).where(Team.id == team_id)
                    t_res = await db_session.execute(stmt)
                    target_t = t_res.scalar_one_or_none()
                    if target_t:
                        await self.team_service._handle_api_error(
                            {"success": False, "error": "兑换成功但 3 次同步均未见成员", "error_code": "ghost_success"},
                            target_t, db_session
                        )
            except Exception as e:
                logger.error(f"后台同步校验发生异常: {e}")


# 创建全局实例
redeem_flow_service = RedeemFlowService()
