"""
质保服务
处理用户质保查询和验证
"""
import logging
import asyncio
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from sqlalchemy import select, and_, or_, delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import RedemptionCode, RedemptionRecord, Team
from app.services.settings import (
    settings_service,
    WARRANTY_EXPIRATION_MODE_REFRESH_ON_REDEEM,
)
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)

# 全局频率限制字典: {(type, key): last_time}
# type: 'email' or 'code'
_query_rate_limit: Dict[Any, datetime] = {}
_QUERY_RATE_LIMIT_WINDOW_SECONDS = 30
_QUERY_RATE_LIMIT_MAX_ENTRIES = 10000


def _prune_query_rate_limit(now: datetime) -> None:
    """清理过期或超量的频率限制条目，避免无界内存增长。"""
    expired_keys = [
        key for key, ts in _query_rate_limit.items()
        if (now - ts).total_seconds() >= _QUERY_RATE_LIMIT_WINDOW_SECONDS
    ]
    for key in expired_keys:
        _query_rate_limit.pop(key, None)

    if len(_query_rate_limit) > _QUERY_RATE_LIMIT_MAX_ENTRIES:
        # 超额时按时间戳先进先出地裁剪
        oldest = sorted(_query_rate_limit.items(), key=lambda kv: kv[1])
        overflow = len(_query_rate_limit) - _QUERY_RATE_LIMIT_MAX_ENTRIES
        for key, _ in oldest[:overflow]:
            _query_rate_limit.pop(key, None)


class WarrantyService:
    """质保服务类"""

    def __init__(self):
        """初始化质保服务"""
        from app.services.team import TeamService
        self.team_service = TeamService()

    async def _get_warranty_start_time(
        self,
        db_session: AsyncSession,
        redemption_code: RedemptionCode,
        reference_record: Optional[RedemptionRecord] = None,
        expiration_mode: Optional[str] = None
    ) -> Optional[datetime]:
        """根据当前模式解析质保起算时间。"""
        mode = expiration_mode or await settings_service.get_warranty_expiration_mode(db_session)

        if mode == WARRANTY_EXPIRATION_MODE_REFRESH_ON_REDEEM:
            return redemption_code.used_at or (reference_record.redeemed_at if reference_record else None)

        result = await db_session.execute(
            select(func.min(RedemptionRecord.redeemed_at))
            .where(RedemptionRecord.code == redemption_code.code)
        )
        first_redeemed_at = result.scalar()

        return first_redeemed_at or redemption_code.used_at or (reference_record.redeemed_at if reference_record else None)

    async def _resolve_warranty_expiry_date(
        self,
        db_session: AsyncSession,
        redemption_code: RedemptionCode,
        reference_record: Optional[RedemptionRecord] = None,
        expiration_mode: Optional[str] = None,
        force_recompute: bool = False,
    ) -> Optional[datetime]:
        """获取质保截止时间，必要时按当前模式动态回退计算。"""
        if not redemption_code.has_warranty:
            return None

        if redemption_code.warranty_expires_at and not force_recompute:
            return redemption_code.warranty_expires_at

        start_time = await self._get_warranty_start_time(
            db_session,
            redemption_code,
            reference_record=reference_record,
            expiration_mode=expiration_mode
        )
        if not start_time:
            return None

        days = redemption_code.warranty_days or 30
        return start_time + timedelta(days=days)

    @staticmethod
    def _is_warranty_valid(redemption_code: RedemptionCode, expiry_date: Optional[datetime]) -> bool:
        """根据截止时间和码状态判断质保是否有效。"""
        if expiry_date:
            return expiry_date >= get_now()

        if redemption_code.has_warranty and redemption_code.status == "unused":
            return True

        return False

    async def check_warranty_status(
        self,
        db_session: AsyncSession,
        email: Optional[str] = None,
        code: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        检查用户质保状态

        Args:
            db_session: 数据库会话
            email: 用户邮箱
            code: 兑换码

        Returns:
            结果字典,包含 success, has_warranty, warranty_valid, warranty_expires_at, 
            banned_teams, can_reuse, original_code, error
        """
        try:
            if not email and not code:
                return {
                    "success": False,
                    "error": "必须提供邮箱或兑换码"
                }

            # 0. 频率限制 (每个邮箱或每个码 30 秒只能查一次)
            now = datetime.now()
            _prune_query_rate_limit(now)
            limit_key = ("email", email) if email else ("code", code)
            last_time = _query_rate_limit.get(limit_key)
            if last_time and (now - last_time).total_seconds() < _QUERY_RATE_LIMIT_WINDOW_SECONDS:
                wait_time = int(_QUERY_RATE_LIMIT_WINDOW_SECONDS - (now - last_time).total_seconds())
                return {
                    "success": False,
                    "error": f"查询太频繁,请 {wait_time} 秒后再试"
                }
            _query_rate_limit[limit_key] = now
            warranty_expiration_mode = await settings_service.get_warranty_expiration_mode(db_session)

            # 1. 查找兑换记录和相关联的 Team, Code
            records_data = []

            if code:
                # 通过兑换码查找所有关联记录
                stmt = (
                    select(RedemptionRecord, RedemptionCode, Team)
                    .options(selectinload(RedemptionRecord.redemption_code), selectinload(RedemptionRecord.team))
                    .join(RedemptionCode, RedemptionRecord.code == RedemptionCode.code)
                    .join(Team, RedemptionRecord.team_id == Team.id)
                    .where(RedemptionCode.code == code)
                    .order_by(RedemptionRecord.redeemed_at.desc())
                )
                result = await db_session.execute(stmt)
                records_data = result.all()

                # 如果没有记录，可能是码还没被使用或不存在
                if not records_data:
                    stmt = select(RedemptionCode).where(RedemptionCode.code == code)
                    result = await db_session.execute(stmt)
                    redemption_code_obj = result.scalar_one_or_none()
                    
                    if not redemption_code_obj:
                        return {
                            "success": True,
                            "has_warranty": False,
                            "warranty_valid": False,
                            "warranty_expires_at": None,
                            "banned_teams": [],
                            "can_reuse": False,
                            "original_code": None,
                            "records": [],
                            "message": "兑换码不存在"
                        }

                    expiry_date = await self._resolve_warranty_expiry_date(
                        db_session,
                        redemption_code_obj,
                        expiration_mode=warranty_expiration_mode
                    )
                    is_valid = self._is_warranty_valid(redemption_code_obj, expiry_date)

                    # 只有码没有记录的情况
                    return {
                        "success": True,
                        "has_warranty": redemption_code_obj.has_warranty,
                        "warranty_valid": is_valid,
                        "warranty_expires_at": expiry_date.isoformat() if expiry_date else None,
                        "banned_teams": [],
                        "can_reuse": False,
                        "original_code": redemption_code_obj.code,
                        "records": [{
                            "code": redemption_code_obj.code,
                            "has_warranty": redemption_code_obj.has_warranty,
                            "warranty_valid": is_valid,
                            "status": redemption_code_obj.status,
                            "used_at": None,
                            "team_id": None,
                            "team_name": None,
                            "team_status": None,
                            "team_expires_at": None,
                            "warranty_expires_at": expiry_date.isoformat() if expiry_date else None
                        }],
                        "message": "兑换码尚未被使用"
                    }

            elif email:
                # 通过邮箱查找所有兑换记录
                stmt = (
                    select(RedemptionRecord, RedemptionCode, Team)
                    .options(selectinload(RedemptionRecord.redemption_code), selectinload(RedemptionRecord.team))
                    .join(RedemptionCode, RedemptionRecord.code == RedemptionCode.code)
                    .join(Team, RedemptionRecord.team_id == Team.id)
                    .where(RedemptionRecord.email == email)
                    .order_by(RedemptionRecord.redeemed_at.desc())
                )
                result = await db_session.execute(stmt)
                all_records = result.all()

                # 只保留每个兑换码的最近一条记录
                seen_codes = set()
                records_data = []
                for row in all_records:
                    # row format: (RedemptionRecord, RedemptionCode, Team)
                    record_obj = row[0]
                    if record_obj.code not in seen_codes:
                        seen_codes.add(record_obj.code)
                        records_data.append(row)

            if not records_data:
                return {
                    "success": True,
                    "has_warranty": False,
                    "warranty_valid": False,
                    "warranty_expires_at": None,
                    "banned_teams": [],
                    "can_reuse": False,
                    "original_code": None,
                    "records": [],
                    "message": "未找到兑换记录"
                }

            # 2. 处理记录并进行必要的实时同步
            final_records = []
            banned_teams_info = []
            has_any_warranty = False
            primary_warranty_valid = False
            primary_expiry = None
            primary_code = None
            can_reuse = False
            suspected_inconsistent_count = 0

            for record, code_obj, team in records_data:
                # 1.1 实时一致性校验 (自愈逻辑)
                # 如果数据库有记录，但 API 列表里没你，说明是虚假成功，直接后台修复
                if team.status != "banned" and team.status != "expired":
                    logger.info(f"质保查询: 正在实时测试 Team {team.id} ({team.team_name}) 的状态")
                    sync_res = await self.team_service.sync_team_info(team.id, db_session)
                    member_emails = [m.lower() for m in sync_res.get("member_emails", [])]
                    
                    if record.email.lower() not in member_emails:
                        expiry_date = await self._resolve_warranty_expiry_date(
                            db_session,
                            code_obj,
                            reference_record=record,
                            expiration_mode=warranty_expiration_mode
                        )
                        is_valid = self._is_warranty_valid(code_obj, expiry_date)
                        if code_obj.has_warranty:
                            has_any_warranty = True
                            if primary_code is None:
                                primary_warranty_valid = is_valid
                                primary_expiry = expiry_date
                                primary_code = code_obj.code
                        logger.warning(
                            f"质保查询发现疑似孤儿记录 (Email: {record.email}, Team: {team.id})，"
                            "为避免误删售后证据，本次仅标记异常，不执行自动清理。"
                        )
                        suspected_inconsistent_count += 1
                        final_records.append({
                            "code": code_obj.code,
                            "has_warranty": code_obj.has_warranty,
                            "warranty_valid": is_valid,
                            "warranty_expires_at": expiry_date.isoformat() if expiry_date else None,
                            "status": code_obj.status,
                            "used_at": record.redeemed_at.isoformat() if record.redeemed_at else None,
                            "team_id": team.id,
                            "team_name": team.team_name,
                            "team_status": "suspected_inconsistent",
                            "team_expires_at": team.expires_at.isoformat() if team.expires_at else None,
                            "email": record.email,
                            "device_code_auth_enabled": team.device_code_auth_enabled
                        })
                        continue

                # 动态计算/提取质保信息
                expiry_date = await self._resolve_warranty_expiry_date(
                    db_session,
                    code_obj,
                    reference_record=record,
                    expiration_mode=warranty_expiration_mode
                )
                is_valid = self._is_warranty_valid(code_obj, expiry_date)

                if code_obj.has_warranty:
                    has_any_warranty = True
                    # 以最近的一个质保码作为主要质保状态参考
                    if primary_code is None:
                        primary_warranty_valid = is_valid
                        primary_expiry = expiry_date
                        primary_code = code_obj.code

                # 记录封号 Team
                if team.status == "banned":
                    banned_teams_info.append({
                        "team_id": team.id,
                        "team_name": team.team_name,
                        "email": team.email,
                        "banned_at": team.last_sync.isoformat() if team.last_sync else None
                    })

                final_records.append({
                    "code": code_obj.code,
                    "has_warranty": code_obj.has_warranty,
                    "warranty_valid": is_valid,
                    "warranty_expires_at": expiry_date.isoformat() if expiry_date else None,
                    "status": code_obj.status,
                    "used_at": record.redeemed_at.isoformat() if record.redeemed_at else None,
                    "team_id": team.id,
                    "team_name": team.team_name,
                    "team_status": team.status,
                    "team_expires_at": team.expires_at.isoformat() if team.expires_at else None,
                    "email": record.email,
                    "device_code_auth_enabled": team.device_code_auth_enabled
                })

            # 3. 判断是否可以重复使用 (只要有有效的质保码且有被封的 Team)
            if has_any_warranty and primary_warranty_valid and len(banned_teams_info) > 0:
                # 进一步验证 (使用现有的 validate_warranty_reuse 逻辑)
                # 这里为了简单直接复用逻辑判断
                can_reuse = True

            # 4. 最终状态判定
            message = "查询成功"
            if has_any_warranty and not final_records and records_data:
                # 这种情况说明刚才所有记录都被自愈逻辑删除了（全是虚假成功）
                message = "系统发现您的兑换记录存在同步异常，已为您自动修复！您的兑换码已恢复，请返回兑换页面重新提交一次即可。"
                can_reuse = True
            elif suspected_inconsistent_count > 0:
                message = "检测到部分兑换记录与远端成员状态不一致；系统已保留原始记录，请联系管理员进一步核查。"

            return {
                "success": True,
                "has_warranty": has_any_warranty,
                "warranty_valid": primary_warranty_valid,
                "warranty_expires_at": primary_expiry.isoformat() if primary_expiry else None,
                "banned_teams": banned_teams_info,
                "can_reuse": can_reuse,
                "original_code": primary_code,
                "records": final_records,
                "message": message
            }

        except Exception as e:
            logger.error(f"检查质保状态失败: {e}")
            return {
                "success": False,
                "error": f"检查质保状态失败: {str(e)}"
            }

    async def scan_expired_warranty_codes(
        self,
        db_session: AsyncSession,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """扫描按当前质保模式已过保、且仍绑定 Team/邮箱的质保码。"""
        try:
            expiration_mode = await settings_service.get_warranty_expiration_mode(db_session)
            stmt = select(RedemptionCode).where(
                RedemptionCode.has_warranty.is_(True),
                RedemptionCode.used_by_email.is_not(None),
                RedemptionCode.used_team_id.is_not(None),
                RedemptionCode.used_at.is_not(None),
            ).order_by(RedemptionCode.used_at.asc(), RedemptionCode.id.asc())
            if limit and limit > 0:
                stmt = stmt.limit(limit)

            result = await db_session.execute(stmt)
            codes = result.scalars().all()

            now = get_now()
            candidates: List[Dict[str, Any]] = []
            for redemption_code in codes:
                expiry_date = await self._resolve_warranty_expiry_date(
                    db_session,
                    redemption_code,
                    expiration_mode=expiration_mode,
                    force_recompute=True,
                )
                if not expiry_date or expiry_date >= now:
                    continue

                normalized_email = self.team_service._normalize_member_email(redemption_code.used_by_email)
                if not normalized_email:
                    continue

                record_count_result = await db_session.execute(
                    select(func.count(RedemptionRecord.id)).where(RedemptionRecord.code == redemption_code.code)
                )
                record_count = int(record_count_result.scalar() or 0)
                candidates.append({
                    "code": redemption_code.code,
                    "email": normalized_email,
                    "team_id": redemption_code.used_team_id,
                    "used_at": redemption_code.used_at.isoformat() if redemption_code.used_at else None,
                    "warranty_expires_at": expiry_date.isoformat(),
                    "record_count": record_count,
                    "expiration_mode": expiration_mode,
                })

            return {
                "success": True,
                "codes": candidates,
                "total": len(candidates),
                "error": None,
            }
        except Exception as e:
            logger.error(f"扫描过保质保码失败: {e}")
            return {
                "success": False,
                "codes": [],
                "total": 0,
                "error": f"扫描过保质保码失败: {str(e)}",
            }

    async def kick_and_destroy_expired_warranty_code(
        self,
        db_session: AsyncSession,
        code: str,
    ) -> Dict[str, Any]:
        """对单个已过保质保码执行踢人与销毁。"""
        try:
            normalized_code = str(code or "").strip()
            if not normalized_code:
                return {"success": False, "code": code, "error": "兑换码不能为空"}

            result = await db_session.execute(
                select(RedemptionCode).where(RedemptionCode.code == normalized_code)
            )
            redemption_code = result.scalar_one_or_none()
            if not redemption_code:
                return {
                    "success": True,
                    "code": normalized_code,
                    "action": "already_destroyed",
                    "message": "兑换码已不存在",
                    "error": None,
                }

            if not redemption_code.has_warranty:
                return {
                    "success": False,
                    "code": normalized_code,
                    "error": "该兑换码不是质保码",
                }

            expiration_mode = await settings_service.get_warranty_expiration_mode(db_session)
            expiry_date = await self._resolve_warranty_expiry_date(
                db_session,
                redemption_code,
                expiration_mode=expiration_mode,
                force_recompute=True,
            )
            if not expiry_date or expiry_date >= get_now():
                return {
                    "success": False,
                    "code": normalized_code,
                    "error": "质保尚未过期",
                }

            team_id = redemption_code.used_team_id
            email = self.team_service._normalize_member_email(redemption_code.used_by_email)
            if not team_id:
                return {
                    "success": False,
                    "code": normalized_code,
                    "error": "兑换码未绑定 Team，无法自动踢人",
                }
            if not email:
                return {
                    "success": False,
                    "code": normalized_code,
                    "error": "兑换码未绑定邮箱，无法自动踢人",
                }

            team_result = await db_session.execute(select(Team).where(Team.id == team_id))
            team = team_result.scalar_one_or_none()
            if not team:
                return {
                    "success": False,
                    "code": normalized_code,
                    "team_id": team_id,
                    "email": email,
                    "error": f"Team ID {team_id} 不存在",
                }

            removal_result = await self.team_service.remove_invite_or_member(team_id, email, db_session)
            removal_message = str(removal_result.get("message") or "")
            removal_error = str(removal_result.get("error") or "")
            removal_success = bool(removal_result.get("success"))
            already_absent = "成员已不存在" in removal_message

            if not removal_success and not already_absent:
                return {
                    "success": False,
                    "code": normalized_code,
                    "team_id": team_id,
                    "email": email,
                    "error": removal_error or removal_message or "移除成员失败",
                }

            from app.services.redemption import redemption_service

            destroy_result = await redemption_service.destroy_code_with_records(normalized_code, db_session)
            if not destroy_result.get("success"):
                return {
                    "success": False,
                    "code": normalized_code,
                    "team_id": team_id,
                    "email": email,
                    "error": destroy_result.get("error") or "销毁兑换码失败",
                }

            return {
                "success": True,
                "code": normalized_code,
                "team_id": team_id,
                "email": email,
                "action": "destroyed_after_absent" if already_absent else "kicked_and_destroyed",
                "message": destroy_result.get("message") or "自动踢人并销毁兑换码成功",
                "destroyed_records": destroy_result.get("deleted_records", 0),
                "warranty_expires_at": expiry_date.isoformat(),
            }
        except Exception as e:
            logger.exception("执行自动踢人并销毁兑换码失败")
            return {
                "success": False,
                "code": code,
                "error": f"执行自动踢人并销毁兑换码失败: {str(e)}",
            }

    async def run_warranty_auto_kick(
        self,
        db_session: AsyncSession,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """扫描并执行整轮质保过期自动踢人任务。"""
        scan_result = await self.scan_expired_warranty_codes(db_session, limit=limit)
        if not scan_result.get("success"):
            return {
                "success": False,
                "scanned": 0,
                "expired_candidates": 0,
                "processed": 0,
                "destroyed": 0,
                "skipped": 0,
                "failed": 0,
                "error": scan_result.get("error") or "扫描失败",
                "results": [],
            }

        candidates = scan_result.get("codes", [])
        results: List[Dict[str, Any]] = []
        processed = 0
        destroyed = 0
        skipped = 0
        failed = 0

        for candidate in candidates:
            item_result = await self.kick_and_destroy_expired_warranty_code(db_session, candidate.get("code", ""))
            results.append(item_result)
            if item_result.get("success"):
                processed += 1
                destroyed += 1
                continue

            if item_result.get("error") == "质保尚未过期":
                skipped += 1
            else:
                failed += 1

        return {
            "success": failed == 0,
            "scanned": scan_result.get("total", 0),
            "expired_candidates": len(candidates),
            "processed": processed,
            "destroyed": destroyed,
            "skipped": skipped,
            "failed": failed,
            "error": None if failed == 0 else "部分过保质保码处理失败",
            "results": results,
        }

    async def validate_warranty_reuse(
        self,
        db_session: AsyncSession,
        code: str,
        email: str
    ) -> Dict[str, Any]:
        """
        验证质保码是否可重复使用

        Args:
            db_session: 数据库会话
            code: 兑换码
            email: 用户邮箱

        Returns:
            结果字典,包含 success, can_reuse, reason, error
        """
        try:
            # 1. 查询兑换码
            stmt = select(RedemptionCode).where(RedemptionCode.code == code)
            result = await db_session.execute(stmt)
            redemption_code = result.scalar_one_or_none()

            if not redemption_code:
                return {
                    "success": True,
                    "can_reuse": False,
                    "reason": "兑换码不存在",
                    "error": None
                }

            # 2. 检查是否为质保码
            if not redemption_code.has_warranty:
                return {
                    "success": True,
                    "can_reuse": False,
                    "reason": "该兑换码不是质保兑换码",
                    "error": None
                }

            # 3. 检查质保期是否有效
            expiry_date = await self._resolve_warranty_expiry_date(db_session, redemption_code)
            if expiry_date and expiry_date < get_now():
                return {
                    "success": True,
                    "can_reuse": False,
                    "reason": "质保已过期",
                    "error": None
                }

            # 4. 检查该兑换码当前是否已有正在使用的活跃 Team (全局检查，不限邮箱)
            # 逻辑：如果该码名下有任何一个 Team 还是 active/full 状态且未过期，则不允许新的激活
            stmt = select(RedemptionRecord).where(RedemptionRecord.code == code)
            result = await db_session.execute(stmt)
            all_records_for_code = result.scalars().all()
            had_matching_history = any(r.email == email for r in all_records_for_code)
            cleaned_orphan_for_email = False
            
            for record in all_records_for_code:
                stmt = select(Team).where(Team.id == record.team_id)
                result = await db_session.execute(stmt)
                team = result.scalar_one_or_none()
                
                if team:
                    is_expired = team.expires_at and team.expires_at < get_now()
                    if team.status in ["active", "full"] and not is_expired:
                        # --- 自愈逻辑：验证是否真的在 Team 中 ---
                        # 针对“虚假成功”导致的拉人记录残留进行清理
                        logger.info(f"验证质保重复使用: 发现活跃 record，正在同步 Team {team.id} 以校验成员是否存在")
                        sync_res = await self.team_service.sync_team_info(team.id, db_session)
                        member_emails = [m.lower() for m in sync_res.get("member_emails", [])]
                        
                        if record.email.lower() not in member_emails:
                            logger.warning(f"自愈逻辑: 发现孤儿记录 (Email: {record.email}, Team: {team.id}), 但同步结果中不包含该成员。正在清理记录。")
                            # 删除该孤儿记录
                            if record.email == email:
                                cleaned_orphan_for_email = True
                            await db_session.delete(record)
                            if not db_session.in_transaction():
                                await db_session.commit()
                            else:
                                await db_session.flush()
                            continue # 继续检查下一个记录或结束循环

                        # 如果是同一个邮箱且确实在 Team 中，提示已在有效 Team 中
                        if record.email == email:
                            return {
                                "success": True,
                                "can_reuse": False,
                                "reason": f"您已在有效 Team 中 ({team.team_name or team.id})，不可重复兑换",
                                "error": None
                            }
                        else:
                            # 如果是不同邮箱，提示已被占用
                            return {
                                "success": True,
                                "can_reuse": False,
                                "reason": "该兑换码当前已被其他账号绑定且正在使用中。如需更换，请确保原账号已下车或原 Team 已失效。",
                                "error": None
                            }

            # 5. 刷新记录列表，避免继续使用已删除的孤儿记录快照
            stmt = select(RedemptionRecord).where(RedemptionRecord.code == code)
            result = await db_session.execute(stmt)
            all_records_for_code = result.scalars().all()

            # 6. 查找当前用户使用该兑换码的记录 (用于后续逻辑判断)
            records = [r for r in all_records_for_code if r.email == email]
            
            if not records:
                if cleaned_orphan_for_email or had_matching_history:
                    return {
                        "success": True,
                        "can_reuse": True,
                        "reason": "检测到历史记录同步异常，已自动修复，可重新兑换",
                        "error": None
                    }
                return {
                    "success": True,
                    "can_reuse": False,
                    "reason": "质保兑换码仅限原使用邮箱申请售后，不支持更名接手",
                    "error": None
                }

            # 7. 检查用户当前是否已在有效的 Team 中
            # 逻辑：如果最近一次加入的 Team 仍然有效（active/full 且未过期），则不允许重复使用
            for record in records:
                stmt = select(Team).where(Team.id == record.team_id)
                result = await db_session.execute(stmt)
                team = result.scalar_one_or_none()
                
                if team:
                    # 如果有任何一个关联 Team 还是 active/full 状态，且未过期
                    is_expired = team.expires_at and team.expires_at < get_now()
                    if team.status in ["active", "full"] and not is_expired:
                        return {
                            "success": True,
                            "can_reuse": False,
                            "reason": f"您已在有效 Team 中 ({team.team_name or team.id})，不可重复兑换",
                            "error": None
                        }

            # 8. 检查是否有过被封的记录
            has_banned_team = False
            for record in records:
                stmt = select(Team).where(Team.id == record.team_id)
                result = await db_session.execute(stmt)
                team = result.scalar_one_or_none()
                if team and team.status == "banned":
                    has_banned_team = True
                    break
            if has_banned_team:
                return {
                    "success": True,
                    "can_reuse": True,
                    "reason": "之前加入的 Team 已封号，可使用质保重复兑换",
                    "error": None
                }
            else:
                return {
                    "success": True,
                    "can_reuse": False,
                    "reason": "未找到被封号记录，且质保不支持正常过期或异常提示的重复兑换",
                    "error": None
                }

        except Exception as e:
            logger.error(f"验证质保码重复使用失败: {e}")
            return {
                "success": False,
                "can_reuse": False,
                "reason": None,
                "error": f"验证失败: {str(e)}"
            }


# 创建全局质保服务实例
warranty_service = WarrantyService()
