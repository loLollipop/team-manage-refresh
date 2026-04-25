import asyncio
import json
import unittest
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import select
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import RedemptionCode, RedemptionRecord, RenewalRequest, Team, TeamEmailMapping, Setting
from app.services.redeem_flow import RedeemFlowService
from app.services.notification import notification_service
from app.services.redemption import RedemptionService
from app.services.settings import settings_service
from app.services.team import TeamService
from app.services.warranty import WarrantyService
from app.routes.admin import generate_welfare_common_code, WelfareCodeGenerateRequest
from app.utils.time_utils import get_now


class StubRedemptionService:
    async def validate_code(self, code, db_session):
        return {
            "success": True,
            "valid": True,
            "redemption_code": {
                "pool_type": "normal",
                "virtual_welfare_code": False,
            },
        }


class StubTeamService:
    def __init__(self, sync_results=None, active_team_ids_by_email=None, reserve_results=None):
        self.sync_results = sync_results or {}
        self.active_team_ids_by_email = {
            str(email).strip().lower(): set(team_ids)
            for email, team_ids in (active_team_ids_by_email or {}).items()
        }
        self.mapping_updates = []
        self.reserve_results = reserve_results or {}
        self.released_team_ids = []

    @staticmethod
    def _normalize_member_email(email):
        if not email:
            return ""
        return str(email).strip().lower()

    async def sync_team_info(self, team_id, db_session):
        team_results = (self.sync_results or {}).get(team_id, [])
        if team_results:
            result = team_results.pop(0)
            if team_results:
                return result
            self.sync_results[team_id] = [result]
            return result

        return {"success": True, "member_emails": [], "error": None}


    async def reserve_seat_if_available(self, team_id, db_session, pool_type="normal"):
        queued = self.reserve_results.get(team_id) or []
        if queued:
            result = queued.pop(0)
            if not queued:
                self.reserve_results[team_id] = [result]
            if result.get("success"):
                team = await db_session.get(Team, team_id)
                if team:
                    team.current_members += 1
                    if team.current_members >= team.max_members:
                        team.status = "full"
                result = {**result, "team": team}
            return result

        team = await db_session.get(Team, team_id)
        if not team or team.pool_type != pool_type or team.status != "active":
            return {"success": False, "error": f"目标 Team {team_id} 不可用"}
        if team.current_members >= team.max_members:
            team.status = "full"
            return {"success": False, "error": "该 Team 已满, 请选择其他 Team 尝试"}

        team.current_members += 1
        if team.current_members >= team.max_members:
            team.status = "full"
        return {"success": True, "team": team, "error": None}

    async def release_reserved_seat(self, team_id, db_session, pool_type="normal"):
        self.released_team_ids.append(team_id)
        team = await db_session.get(Team, team_id)
        if team and team.current_members > 0:
            team.current_members -= 1
            if team.current_members >= team.max_members:
                team.status = "full"
            else:
                team.status = "active"

    async def _handle_api_error(self, result, team, db_session):
        error_code = result.get("error_code")
        error_msg = str(result.get("error", "")).lower()

        if error_code in {"account_deactivated", "token_invalidated"}:
            team.status = "banned"
            await db_session.commit()
            return True

        if any(keyword in error_msg for keyword in ["token has been invalidated", "deactivated", "suspended", "not found", "deleted"]):
            team.status = "banned"
            await db_session.commit()
            return True

        if any(keyword in error_msg for keyword in ["maximum number of seats", "full", "no seats"]):
            team.status = "full"
            await db_session.commit()
            return True

        await db_session.commit()
        return False

    async def ensure_access_token(self, team, db_session):
        return "token"

    async def get_active_team_ids_for_email(self, email, db_session, pool_type=None):
        normalized_email = str(email).strip().lower()
        return sorted(self.active_team_ids_by_email.get(normalized_email, set()))

    async def upsert_team_email_mapping(self, team_id, email, status, db_session, source="sync"):
        normalized_email = str(email).strip().lower()
        self.mapping_updates.append((team_id, normalized_email, status, source))
        active_team_ids = self.active_team_ids_by_email.setdefault(normalized_email, set())
        if status in {"joined", "invited"}:
            active_team_ids.add(team_id)
        else:
            active_team_ids.discard(team_id)
        return None


class StubChatGPTService:
    def __init__(self, invite_results):
        self.invite_results = invite_results

    async def send_invite(self, access_token, account_id, email, db_session, identifier="default"):
        team_results = self.invite_results.get(account_id, [])
        if team_results:
            result = team_results.pop(0)
            if team_results:
                return result
            self.invite_results[account_id] = [result]
            return result

        return {"success": True, "data": {"account_invites": [{"email": email}]}}


class WarrantyAutoKickTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        settings_service.clear_cache()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(__import__("sqlalchemy").text("PRAGMA foreign_keys=ON"))

    async def asyncTearDown(self):
        settings_service.clear_cache()
        await self.engine.dispose()

    async def test_scan_expired_warranty_codes_recomputes_history_using_current_mode(self):
        async with self.session_factory() as session:
            team = Team(
                id=201,
                email="owner@example.com",
                access_token_encrypted="token-1",
                account_id="acct-auto-kick",
                team_name="Warranty Team",
                current_members=1,
                max_members=5,
                status="active",
                pool_type="normal",
            )
            session.add(team)
            await session.commit()

            code = RedemptionCode(
                code="WARRANTY-RECALC-001",
                status="used",
                has_warranty=True,
                warranty_days=30,
                used_by_email="user@example.com",
                used_team_id=201,
                used_at=get_now() - timedelta(days=1),
                warranty_expires_at=get_now() + timedelta(days=20),
            )
            first_record = RedemptionRecord(
                email="user@example.com",
                code="WARRANTY-RECALC-001",
                team_id=201,
                account_id="acct-auto-kick",
                redeemed_at=get_now() - timedelta(days=40),
                is_warranty_redemption=True,
            )
            second_record = RedemptionRecord(
                email="user@example.com",
                code="WARRANTY-RECALC-001",
                team_id=201,
                account_id="acct-auto-kick",
                redeemed_at=get_now() - timedelta(days=1),
                is_warranty_redemption=True,
            )
            session.add_all([
                code,
                first_record,
                second_record,
                Setting(key="warranty_expiration_mode", value="first_use"),
            ])
            await session.commit()

            service = WarrantyService()
            result = await service.scan_expired_warranty_codes(session)

            self.assertTrue(result["success"])
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["codes"][0]["code"], "WARRANTY-RECALC-001")

    async def test_kick_and_destroy_expired_warranty_code_removes_records_and_code(self):
        async with self.session_factory() as session:
            team = Team(
                id=202,
                email="owner@example.com",
                access_token_encrypted="token-1",
                account_id="acct-auto-kick-2",
                team_name="Kick Team",
                current_members=1,
                max_members=5,
                status="active",
                pool_type="normal",
            )
            session.add(team)
            await session.commit()

            code = RedemptionCode(
                code="WARRANTY-KICK-001",
                status="used",
                has_warranty=True,
                warranty_days=30,
                used_by_email="member@example.com",
                used_team_id=202,
                used_at=get_now() - timedelta(days=35),
                warranty_expires_at=get_now() - timedelta(days=5),
            )
            record = RedemptionRecord(
                email="member@example.com",
                code="WARRANTY-KICK-001",
                team_id=202,
                account_id="acct-auto-kick-2",
                redeemed_at=get_now() - timedelta(days=35),
                is_warranty_redemption=True,
            )
            session.add_all([
                code,
                record,
                Setting(key="warranty_expiration_mode", value="refresh_on_redeem"),
            ])
            await session.commit()

            service = WarrantyService()

            async def stub_remove_invite_or_member(team_id, email, db_session):
                self.assertEqual(team_id, 202)
                self.assertEqual(email, "member@example.com")
                return {"success": True, "message": "成员已删除", "error": None}

            service.team_service.remove_invite_or_member = stub_remove_invite_or_member

            result = await service.kick_and_destroy_expired_warranty_code(session, "WARRANTY-KICK-001")

            self.assertTrue(result["success"])
            self.assertEqual(result["action"], "kicked_and_destroyed")

            remaining_code = await session.execute(
                select(RedemptionCode).where(RedemptionCode.code == "WARRANTY-KICK-001")
            )
            self.assertIsNone(remaining_code.scalar_one_or_none())

            remaining_records = await session.execute(
                select(RedemptionRecord).where(RedemptionRecord.code == "WARRANTY-KICK-001")
            )
            self.assertEqual(list(remaining_records.scalars().all()), [])

    async def test_extension_days_prevent_auto_kick_until_extended_expiry(self):
        async with self.session_factory() as session:
            team = Team(
                id=202,
                email="owner@example.com",
                access_token_encrypted="token-1",
                account_id="acct-auto-kick-keep",
                team_name="Extended Team",
                current_members=1,
                max_members=5,
                status="active",
                pool_type="normal",
            )
            session.add(team)
            await session.commit()

            code = RedemptionCode(
                code="WARRANTY-EXTEND-001",
                status="used",
                has_warranty=True,
                warranty_days=30,
                extension_days=10,
                used_by_email="extend@example.com",
                used_team_id=202,
                used_at=get_now() - timedelta(days=35),
                warranty_expires_at=get_now() + timedelta(days=5),
            )
            session.add_all([
                code,
                Setting(key="warranty_expiration_mode", value="refresh_on_redeem"),
            ])
            await session.commit()

            service = WarrantyService()
            result = await service.scan_expired_warranty_codes(session)
            self.assertTrue(result["success"])
            self.assertEqual(result["total"], 0)

    async def test_create_and_extend_renewal_request_updates_expiry(self):
        async with self.session_factory() as session:
            team = Team(
                id=301,
                email="owner@example.com",
                access_token_encrypted="token-1",
                account_id="acct-renew-request",
                team_name="Renew Request Team",
                current_members=1,
                max_members=5,
                status="active",
                pool_type="normal",
            )
            session.add(team)
            await session.commit()

            code = RedemptionCode(
                code="WARRANTY-REQUEST-001",
                status="used",
                has_warranty=True,
                warranty_days=30,
                extension_days=0,
                used_by_email="request@example.com",
                used_team_id=301,
                used_at=get_now() - timedelta(days=27),
                warranty_expires_at=get_now() + timedelta(days=3),
            )
            session.add_all([
                code,
                Setting(key="warranty_expiration_mode", value="refresh_on_redeem"),
                RedemptionRecord(
                    email="request@example.com",
                    code="WARRANTY-REQUEST-001",
                    team_id=301,
                    account_id="acct-renew-request",
                    is_warranty_redemption=True,
                ),
            ])
            await session.commit()

            service = WarrantyService()
            create_result = await service.create_renewal_request(
                session,
                email="request@example.com",
                code="WARRANTY-REQUEST-001",
                team_id=301,
            )
            self.assertTrue(create_result["success"])

            list_result = await service.get_renewal_requests(session, status_filter="pending")
            self.assertEqual(list_result["pending_count"], 1)
            request_id = list_result["requests"][0]["id"]

            extend_result = await service.extend_warranty_request(
                session,
                request_id=request_id,
                extension_days=7,
            )
            self.assertTrue(extend_result["success"])
            self.assertEqual(extend_result["remaining_warranty_days"], 10)

            updated_code = await session.execute(
                select(RedemptionCode).where(RedemptionCode.code == "WARRANTY-REQUEST-001")
            )
            updated_code_obj = updated_code.scalar_one()
            self.assertEqual(updated_code_obj.extension_days, 7)

            updated_request = await session.execute(
                select(RenewalRequest).where(RenewalRequest.id == request_id)
            )
            updated_request_obj = updated_request.scalar_one()
            self.assertEqual(updated_request_obj.status, "extended")
            self.assertEqual(updated_request_obj.extension_days, 7)

    async def test_auto_kick_dismisses_pending_renewal_request(self):
        """用户提交续期请求但未被审批时，自动踢人仍然执行，并将该请求标记为 ignored。"""
        async with self.session_factory() as session:
            team = Team(
                id=410,
                email="owner@example.com",
                access_token_encrypted="token-1",
                account_id="acct-auto-kick-renewal",
                team_name="Auto Kick Renewal Team",
                current_members=1,
                max_members=5,
                status="active",
                pool_type="normal",
            )
            session.add(team)
            await session.commit()

            code = RedemptionCode(
                code="WARRANTY-AK-RENEWAL-001",
                status="used",
                has_warranty=True,
                warranty_days=30,
                used_by_email="cheater@example.com",
                used_team_id=410,
                used_at=get_now() - timedelta(days=40),
                warranty_expires_at=get_now() - timedelta(days=10),
            )
            renewal = RenewalRequest(
                email="cheater@example.com",
                code="WARRANTY-AK-RENEWAL-001",
                team_id=410,
                status="pending",
            )
            session.add_all([
                code,
                renewal,
                Setting(key="warranty_expiration_mode", value="refresh_on_redeem"),
            ])
            await session.commit()
            renewal_id = renewal.id

            service = WarrantyService()

            async def stub_remove_invite_or_member(team_id, email, db_session):
                self.assertEqual(team_id, 410)
                self.assertEqual(email, "cheater@example.com")
                return {"success": True, "message": "成员已删除", "error": None}

            service.team_service.remove_invite_or_member = stub_remove_invite_or_member

            result = await service.kick_and_destroy_expired_warranty_code(session, "WARRANTY-AK-RENEWAL-001")
            self.assertTrue(result["success"])
            self.assertEqual(result["category"], "destroyed")
            self.assertEqual(result.get("dismissed_renewal_requests"), 1)

            remaining_code = await session.execute(
                select(RedemptionCode).where(RedemptionCode.code == "WARRANTY-AK-RENEWAL-001")
            )
            self.assertIsNone(remaining_code.scalar_one_or_none())

            updated_renewal = await session.execute(
                select(RenewalRequest).where(RenewalRequest.id == renewal_id)
            )
            self.assertIsNone(updated_renewal.scalar_one_or_none())

            # admin 待处理列表中不再出现该续期请求
            list_result = await service.get_renewal_requests(session, status_filter="pending")
            self.assertEqual(list_result["pending_count"], 0)

    async def test_run_warranty_auto_kick_categorizes_results(self):
        """整轮任务：destroyed / skipped / failed 按类别独立统计。"""
        async with self.session_factory() as session:
            team = Team(
                id=420,
                email="owner@example.com",
                access_token_encrypted="token-1",
                account_id="acct-run-auto-kick",
                team_name="Run Auto Kick Team",
                current_members=1,
                max_members=5,
                status="active",
                pool_type="normal",
            )
            session.add(team)
            await session.commit()

            expired_code = RedemptionCode(
                code="WARRANTY-RUN-OK",
                status="used",
                has_warranty=True,
                warranty_days=30,
                used_by_email="ok@example.com",
                used_team_id=420,
                used_at=get_now() - timedelta(days=40),
                warranty_expires_at=get_now() - timedelta(days=10),
            )
            session.add_all([
                expired_code,
                Setting(key="warranty_expiration_mode", value="refresh_on_redeem"),
            ])
            await session.commit()

            service = WarrantyService()

            async def stub_remove(team_id, email, db_session):
                return {"success": True, "message": "成员已删除", "error": None}

            service.team_service.remove_invite_or_member = stub_remove

            stats = await service.run_warranty_auto_kick(session)
            self.assertTrue(stats["success"])
            self.assertEqual(stats["destroyed"], 1)
            self.assertEqual(stats["skipped"], 0)
            self.assertEqual(stats["failed"], 0)

    async def test_create_renewal_request_rejects_without_redemption_record(self):
        """没有 (email, code) 对应的兑换记录时不允许提交续期，避免任意人灌爆待办列表。"""
        async with self.session_factory() as session:
            team = Team(
                id=601,
                email="owner@example.com",
                access_token_encrypted="token",
                account_id="acct-real",
                team_name="Real Team",
                current_members=1,
                max_members=5,
                status="active",
                pool_type="normal",
            )
            session.add(team)
            await session.commit()

            code = RedemptionCode(
                code="WARRANTY-OWN-CHECK-001",
                status="used",
                has_warranty=True,
                warranty_days=30,
                used_by_email="real@example.com",
                used_team_id=601,
                used_at=get_now() - timedelta(days=10),
                warranty_expires_at=get_now() + timedelta(days=20),
            )
            session.add_all([
                code,
                Setting(key="warranty_expiration_mode", value="refresh_on_redeem"),
                RedemptionRecord(
                    email="real@example.com",
                    code="WARRANTY-OWN-CHECK-001",
                    team_id=601,
                    account_id="acct-real",
                    is_warranty_redemption=True,
                ),
            ])
            await session.commit()

            service = WarrantyService()
            attacker = await service.create_renewal_request(
                session,
                email="attacker@example.com",
                code="WARRANTY-OWN-CHECK-001",
            )
            self.assertFalse(attacker["success"])
            self.assertIn("未使用", attacker.get("error", ""))

            owner = await service.create_renewal_request(
                session,
                email="REAL@example.com",  # 大小写差异不影响归属判定
                code="WARRANTY-OWN-CHECK-001",
            )
            self.assertTrue(owner["success"])

    async def test_create_renewal_request_rejects_non_warranty_code(self):
        """普通码不参与续期，避免管理员误处理。"""
        async with self.session_factory() as session:
            team = Team(
                id=602,
                email="owner@example.com",
                access_token_encrypted="token",
                account_id="acct-normal",
                team_name="Normal Team",
                current_members=1,
                max_members=5,
                status="active",
                pool_type="normal",
            )
            session.add(team)
            await session.commit()

            code = RedemptionCode(
                code="NORMAL-001",
                status="used",
                has_warranty=False,
                warranty_days=0,
            )
            session.add_all([
                code,
                RedemptionRecord(
                    email="user@example.com",
                    code="NORMAL-001",
                    team_id=602,
                    account_id="acct-normal",
                    is_warranty_redemption=False,
                ),
            ])
            await session.commit()

            service = WarrantyService()
            result = await service.create_renewal_request(
                session,
                email="user@example.com",
                code="NORMAL-001",
            )
            self.assertFalse(result["success"])
            self.assertIn("质保", result.get("error", ""))

    async def test_extend_warranty_request_rejects_repeat_handling(self):
        """同一管理员快速双击 / 多 worker 同时点 extend，不允许把已处理的请求再叠加一次扣减。"""
        async with self.session_factory() as session:
            team = Team(
                id=603,
                email="owner@example.com",
                access_token_encrypted="token",
                account_id="acct-idemp",
                team_name="Idempotent Team",
                current_members=1,
                max_members=5,
                status="active",
                pool_type="normal",
            )
            session.add(team)
            await session.commit()

            code = RedemptionCode(
                code="WARRANTY-IDEMP-001",
                status="used",
                has_warranty=True,
                warranty_days=30,
                extension_days=0,
                used_by_email="user@example.com",
                used_team_id=603,
                used_at=get_now() - timedelta(days=5),
                warranty_expires_at=get_now() + timedelta(days=25),
            )
            renewal = RenewalRequest(
                email="user@example.com",
                code="WARRANTY-IDEMP-001",
                team_id=603,
                status="pending",
            )
            session.add_all([
                code,
                renewal,
                Setting(key="warranty_expiration_mode", value="refresh_on_redeem"),
                RedemptionRecord(
                    email="user@example.com",
                    code="WARRANTY-IDEMP-001",
                    team_id=603,
                    account_id="acct-idemp",
                    is_warranty_redemption=True,
                ),
            ])
            await session.commit()
            renewal_id = renewal.id

            service = WarrantyService()
            first = await service.extend_warranty_request(session, request_id=renewal_id, extension_days=10)
            self.assertTrue(first["success"])

            second = await service.extend_warranty_request(session, request_id=renewal_id, extension_days=10)
            self.assertFalse(second["success"])

            third = await service.ignore_renewal_request(session, request_id=renewal_id)
            self.assertFalse(third["success"])

            updated_code = await session.execute(
                select(RedemptionCode).where(RedemptionCode.code == "WARRANTY-IDEMP-001")
            )
            self.assertEqual(updated_code.scalar_one().extension_days, 10)

    async def test_clear_code_usage_state_resets_extension_days(self):
        """所有记录被撤回后 extension_days 必须清零，避免下一个用户白嫖剩余天数。"""
        from app.services.redemption import RedemptionService
        async with self.session_factory() as session:
            code = RedemptionCode(
                code="WARRANTY-EXTRESET-001",
                status="used",
                has_warranty=True,
                warranty_days=30,
                extension_days=15,
                used_by_email="prev@example.com",
                used_team_id=None,
                used_at=get_now() - timedelta(days=3),
                warranty_expires_at=get_now() + timedelta(days=42),
            )
            session.add(code)
            await session.commit()

            RedemptionService._clear_code_usage_state(code)
            await session.commit()

            refreshed = await session.execute(
                select(RedemptionCode).where(RedemptionCode.code == "WARRANTY-EXTRESET-001")
            )
            row = refreshed.scalar_one()
            self.assertEqual(row.extension_days, 0)
            self.assertIsNone(row.used_by_email)
            self.assertEqual(row.status, "unused")

    async def test_admin_delete_code_cleans_up_pending_renewal_requests(self):
        """admin 直接删除无记录的兑换码时，需要先清掉关联的续期请求，避免 FK 失败/孤儿。"""
        from app.services.redemption import RedemptionService

        async with self.session_factory() as session:
            code = RedemptionCode(
                code="WARRANTY-DELCODE-001",
                status="unused",
                has_warranty=True,
                warranty_days=30,
            )
            renewal = RenewalRequest(
                email="user@example.com",
                code="WARRANTY-DELCODE-001",
                team_id=None,
                status="pending",
            )
            session.add_all([code, renewal])
            await session.commit()
            renewal_id = renewal.id

            redemption_service = RedemptionService()
            result = await redemption_service.delete_code("WARRANTY-DELCODE-001", session)
            self.assertTrue(result["success"])

            remaining_code = await session.execute(
                select(RedemptionCode).where(RedemptionCode.code == "WARRANTY-DELCODE-001")
            )
            self.assertIsNone(remaining_code.scalar_one_or_none())

            remaining_renewal = await session.execute(
                select(RenewalRequest).where(RenewalRequest.id == renewal_id)
            )
            self.assertIsNone(remaining_renewal.scalar_one_or_none())

    async def test_auto_kick_preserves_extended_renewal_history(self):
        """自动踢人销毁兑换码时也只清 pending 续期请求；extended/ignored 留下作为审计证据。"""
        async with self.session_factory() as session:
            team = Team(
                id=440,
                email="owner@example.com",
                access_token_encrypted="token-1",
                account_id="acct-auto-kick-audit",
                team_name="Auto Kick Audit Team",
                current_members=1,
                max_members=5,
                status="active",
                pool_type="normal",
            )
            session.add(team)
            await session.commit()

            code = RedemptionCode(
                code="WARRANTY-AK-AUDIT-001",
                status="used",
                has_warranty=True,
                warranty_days=30,
                used_by_email="user@example.com",
                used_team_id=440,
                used_at=get_now() - timedelta(days=40),
                warranty_expires_at=get_now() - timedelta(days=10),
            )
            extended = RenewalRequest(
                email="user@example.com",
                code="WARRANTY-AK-AUDIT-001",
                team_id=440,
                status="extended",
                extension_days=10,
                admin_note="第一次投诉，续 10 天",
            )
            pending = RenewalRequest(
                email="user@example.com",
                code="WARRANTY-AK-AUDIT-001",
                team_id=440,
                status="pending",
            )
            session.add_all([
                code, extended, pending,
                Setting(key="warranty_expiration_mode", value="refresh_on_redeem"),
            ])
            await session.commit()
            extended_id = extended.id
            pending_id = pending.id

            service = WarrantyService()
            async def _stub_remove(team_id, email, db_session):
                return {"success": True, "message": "ok", "error": None}
            service.team_service.remove_invite_or_member = _stub_remove

            result = await service.kick_and_destroy_expired_warranty_code(
                session, "WARRANTY-AK-AUDIT-001"
            )
            self.assertTrue(result["success"])
            self.assertEqual(result["category"], "destroyed")

            # pending 应被物理删除
            self.assertIsNone(
                (await session.execute(
                    select(RenewalRequest).where(RenewalRequest.id == pending_id)
                )).scalar_one_or_none()
            )

            # extended 必须保留，code 被置 NULL，admin_note 追加销毁标记
            extended_after = (await session.execute(
                select(RenewalRequest).where(RenewalRequest.id == extended_id)
            )).scalar_one_or_none()
            self.assertIsNotNone(extended_after)
            self.assertIsNone(extended_after.code)
            self.assertIn("WARRANTY-AK-AUDIT-001", extended_after.admin_note)
            self.assertIn("销毁", extended_after.admin_note)
            self.assertIn("第一次投诉", extended_after.admin_note)
            self.assertEqual(extended_after.extension_days, 10)

    async def test_destroy_preserves_extended_and_ignored_renewal_history(self):
        """销毁兑换码时只删 pending 续期请求；extended/ignored 必须保留作为审计证据。"""
        from app.services.redemption import RedemptionService

        async with self.session_factory() as session:
            code = RedemptionCode(
                code="WARRANTY-AUDIT-001",
                status="unused",
                has_warranty=True,
                warranty_days=30,
            )
            extended_renewal = RenewalRequest(
                email="alice@example.com",
                code="WARRANTY-AUDIT-001",
                team_id=None,
                status="extended",
                extension_days=15,
                admin_note="客户投诉，续 15 天",
            )
            ignored_renewal = RenewalRequest(
                email="bob@example.com",
                code="WARRANTY-AUDIT-001",
                team_id=None,
                status="ignored",
                admin_note="频繁刷续期，忽略",
            )
            pending_renewal = RenewalRequest(
                email="carol@example.com",
                code="WARRANTY-AUDIT-001",
                team_id=None,
                status="pending",
            )
            session.add_all([code, extended_renewal, ignored_renewal, pending_renewal])
            await session.commit()
            extended_id = extended_renewal.id
            ignored_id = ignored_renewal.id
            pending_id = pending_renewal.id

            redemption_service = RedemptionService()
            result = await redemption_service.delete_code("WARRANTY-AUDIT-001", session)
            self.assertTrue(result["success"])

            # pending 应当被物理删除
            self.assertIsNone(
                (await session.execute(
                    select(RenewalRequest).where(RenewalRequest.id == pending_id)
                )).scalar_one_or_none()
            )

            # extended / ignored 必须保留
            extended_after = (await session.execute(
                select(RenewalRequest).where(RenewalRequest.id == extended_id)
            )).scalar_one_or_none()
            self.assertIsNotNone(extended_after)
            self.assertIn("已于", extended_after.admin_note)
            self.assertIn("销毁", extended_after.admin_note)
            self.assertIn("客户投诉", extended_after.admin_note)
            self.assertEqual(extended_after.extension_days, 15)

            ignored_after = (await session.execute(
                select(RenewalRequest).where(RenewalRequest.id == ignored_id)
            )).scalar_one_or_none()
            self.assertIsNotNone(ignored_after)
            self.assertIn("频繁刷续期", ignored_after.admin_note)
            self.assertIn("销毁", ignored_after.admin_note)

    async def test_bulk_update_codes_recomputes_warranty_expiry(self):
        """改 warranty_days 时已用码的 warranty_expires_at 必须同步重算，避免 UI 与定时踢人脱钩。"""
        from app.services.redemption import RedemptionService

        async with self.session_factory() as session:
            used_at = get_now() - timedelta(days=10)
            code = RedemptionCode(
                code="WARRANTY-BULK-001",
                status="used",
                has_warranty=True,
                warranty_days=30,
                used_by_email="user@example.com",
                used_team_id=None,
                used_at=used_at,
                warranty_expires_at=used_at + timedelta(days=30),
            )
            session.add_all([
                code,
                Setting(key="warranty_expiration_mode", value="refresh_on_redeem"),
            ])
            await session.commit()

            redemption_service = RedemptionService()
            result = await redemption_service.bulk_update_codes(
                ["WARRANTY-BULK-001"], session, warranty_days=60
            )
            self.assertTrue(result["success"])

            refreshed = (await session.execute(
                select(RedemptionCode).where(RedemptionCode.code == "WARRANTY-BULK-001")
            )).scalar_one()
            self.assertEqual(refreshed.warranty_days, 60)
            # used_at + 60 天，允许 1 秒抖动
            expected = used_at + timedelta(days=60)
            delta_seconds = abs((refreshed.warranty_expires_at - expected).total_seconds())
            self.assertLess(delta_seconds, 5)

    async def test_bulk_update_codes_clears_expiry_when_warranty_disabled(self):
        """关掉 has_warranty 时，已用码的 warranty_expires_at 必须清掉，避免 UI 仍显示"未到期"。"""
        from app.services.redemption import RedemptionService

        async with self.session_factory() as session:
            used_at = get_now() - timedelta(days=5)
            code = RedemptionCode(
                code="WARRANTY-BULK-OFF-001",
                status="used",
                has_warranty=True,
                warranty_days=30,
                used_by_email="user@example.com",
                used_team_id=None,
                used_at=used_at,
                warranty_expires_at=used_at + timedelta(days=30),
            )
            session.add_all([
                code,
                Setting(key="warranty_expiration_mode", value="refresh_on_redeem"),
            ])
            await session.commit()

            redemption_service = RedemptionService()
            result = await redemption_service.bulk_update_codes(
                ["WARRANTY-BULK-OFF-001"], session, has_warranty=False
            )
            self.assertTrue(result["success"])

            refreshed = (await session.execute(
                select(RedemptionCode).where(RedemptionCode.code == "WARRANTY-BULK-OFF-001")
            )).scalar_one()
            self.assertFalse(refreshed.has_warranty)
            self.assertIsNone(refreshed.warranty_expires_at)

    async def test_kick_skips_when_team_missing_and_destroys_code(self):
        """Team 已被管理员删除时不应当作 failed，应直接销毁兑换码。"""
        async with self.session_factory() as session:
            team = Team(
                id=430,
                email="owner@example.com",
                access_token_encrypted="token-1",
                account_id="acct-missing-team",
                team_name="To Be Deleted",
                current_members=1,
                max_members=5,
                status="active",
                pool_type="normal",
            )
            session.add(team)
            await session.commit()

            code = RedemptionCode(
                code="WARRANTY-AK-MISSING-TEAM",
                status="used",
                has_warranty=True,
                warranty_days=30,
                used_by_email="ghost@example.com",
                used_team_id=430,
                used_at=get_now() - timedelta(days=40),
                warranty_expires_at=get_now() - timedelta(days=10),
            )
            session.add_all([
                code,
                Setting(key="warranty_expiration_mode", value="refresh_on_redeem"),
            ])
            await session.commit()

            # 模拟 Team 已被管理员手动删除（保持 used_team_id 指向已不存在的 ID），
            # 暂时关闭 FK 约束以便构造这种历史脏数据。
            sa = __import__("sqlalchemy")
            await session.execute(sa.text("PRAGMA foreign_keys = OFF"))
            await session.execute(sa.text("DELETE FROM teams WHERE id = 430"))
            await session.commit()
            await session.execute(sa.text("PRAGMA foreign_keys = ON"))

            service = WarrantyService()

            async def fail_remove(*args, **kwargs):
                self.fail("Team 不存在时不应调用 remove_invite_or_member")

            service.team_service.remove_invite_or_member = fail_remove

            result = await service.kick_and_destroy_expired_warranty_code(
                session, "WARRANTY-AK-MISSING-TEAM"
            )
            self.assertTrue(result["success"])
            self.assertEqual(result["category"], "destroyed")
            self.assertEqual(result["action"], "destroyed_after_team_missing")

            remaining_code = await session.execute(
                select(RedemptionCode).where(RedemptionCode.code == "WARRANTY-AK-MISSING-TEAM")
            )
            self.assertIsNone(remaining_code.scalar_one_or_none())


class TeamServiceBulkInviteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(__import__("sqlalchemy").text("PRAGMA foreign_keys=ON"))

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _seed_team(self, *, current_members=1, max_members=5, status="active"):
        async with self.session_factory() as session:
            team = Team(
                id=101,
                email="owner@example.com",
                access_token_encrypted="token-1",
                account_id="acct-bulk",
                team_name="Bulk Team",
                current_members=current_members,
                max_members=max_members,
                status=status,
                pool_type="normal",
            )
            session.add(team)
            await session.commit()

    @staticmethod
    async def _return_token(*args, **kwargs):
        return "token"

    @staticmethod
    async def _noop_reset(*args, **kwargs):
        return None

    async def test_add_team_members_filters_invalid_duplicate_and_existing(self):
        await self._seed_team(current_members=1, max_members=5)
        team_service = TeamService()

        async def stub_sync(team_id, db_session, force_refresh=False):
            team = await db_session.get(Team, team_id)
            return {
                "success": True,
                "message": f"同步成功,当前成员数: {team.current_members}",
                "member_emails": ["existing@example.com"],
                "error": None,
            }

        async def stub_add_member(team_id, email, db_session):
            return {
                "success": True,
                "message": f"邀请已发送到 {email}",
                "error": None,
                "status": "invited",
                "email": email,
            }

        async with self.session_factory() as session:
            with patch.object(team_service, "sync_team_info", new=stub_sync), \
                 patch.object(team_service, "add_team_member", new=stub_add_member):
                result = await team_service.add_team_members(
                    101,
                    ["valid@example.com", "bad-email", "VALID@example.com", "existing@example.com"],
                    session,
                )

            self.assertTrue(result["success"])
            self.assertTrue(result["partial_success"])
            self.assertEqual(result["summary"]["submitted"], 4)
            self.assertEqual(result["summary"]["unique"], 2)
            self.assertEqual(result["summary"]["invited"], 1)
            self.assertEqual(result["summary"]["invalid"], 1)
            self.assertEqual(result["summary"]["duplicate"], 1)
            self.assertEqual(result["summary"]["already_exists"], 1)
            self.assertEqual(result["summary"]["failed"], 0)
            self.assertEqual(result["summary"]["no_seat"], 0)

            statuses = {item["email"]: item["status"] for item in result["results"]}
            self.assertEqual(statuses["valid@example.com"], "invited")
            self.assertEqual(statuses["bad-email"], "invalid_email")
            self.assertEqual(statuses["existing@example.com"], "already_exists")

    async def test_add_team_members_marks_no_seat_for_overflow(self):
        await self._seed_team(current_members=4, max_members=5)
        team_service = TeamService()
        add_member_calls = []

        async def stub_sync(team_id, db_session, force_refresh=False):
            team = await db_session.get(Team, team_id)
            return {
                "success": True,
                "message": f"同步成功,当前成员数: {team.current_members}",
                "member_emails": [],
                "error": None,
            }

        async def stub_add_member(team_id, email, db_session):
            add_member_calls.append(email)
            return {
                "success": True,
                "message": f"邀请已发送到 {email}",
                "error": None,
                "status": "invited",
                "email": email,
            }

        async with self.session_factory() as session:
            with patch.object(team_service, "sync_team_info", new=stub_sync), \
                 patch.object(team_service, "add_team_member", new=stub_add_member):
                result = await team_service.add_team_members(
                    101,
                    ["first@example.com", "second@example.com"],
                    session,
                )

            self.assertTrue(result["success"])
            self.assertTrue(result["partial_success"])
            self.assertEqual(add_member_calls, ["first@example.com"])
            self.assertEqual(result["summary"]["invited"], 1)
            self.assertEqual(result["summary"]["no_seat"], 1)
            statuses = {item["email"]: item["status"] for item in result["results"]}
            self.assertEqual(statuses["second@example.com"], "no_seat")

    async def test_add_team_members_stops_after_fatal_error(self):
        await self._seed_team(current_members=1, max_members=5)
        team_service = TeamService()
        invited_calls = []

        async def stub_sync(team_id, db_session, force_refresh=False):
            team = await db_session.get(Team, team_id)
            return {
                "success": True,
                "message": f"同步成功,当前成员数: {team.current_members}",
                "member_emails": [],
                "error": None,
            }

        async def stub_add_member(team_id, email, db_session):
            invited_calls.append(email)
            if email == "fatal@example.com":
                return {
                    "success": False,
                    "message": None,
                    "error": "Token 已失效 (token_invalidated)",
                    "error_code": "token_invalidated",
                    "status": "failed",
                    "email": email,
                }
            return {
                "success": True,
                "message": f"邀请已发送到 {email}",
                "error": None,
                "status": "invited",
                "email": email,
            }

        async with self.session_factory() as session:
            with patch.object(team_service, "sync_team_info", new=stub_sync), \
                 patch.object(team_service, "add_team_member", new=stub_add_member):
                result = await team_service.add_team_members(
                    101,
                    ["first@example.com", "fatal@example.com", "later@example.com"],
                    session,
                )

            self.assertTrue(result["success"])
            self.assertTrue(result["partial_success"])
            self.assertEqual(invited_calls, ["first@example.com", "fatal@example.com"])
            self.assertEqual(result["summary"]["invited"], 1)
            self.assertEqual(result["summary"]["failed"], 1)
            self.assertEqual(result["summary"]["not_processed"], 1)
            self.assertEqual(result["results"][-1]["status"], "not_processed")


class RedeemFlowServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(__import__("sqlalchemy").text("PRAGMA foreign_keys=ON"))

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _seed_basic_data(self):
        async with self.session_factory() as session:
            team_1 = Team(
                id=1,
                email="owner-1@example.com",
                access_token_encrypted="token-1",
                account_id="acct-1",
                team_name="Team 1",
                current_members=3,
                max_members=6,
                status="active",
                pool_type="normal",
            )
            team_2 = Team(
                id=2,
                email="owner-2@example.com",
                access_token_encrypted="token-2",
                account_id="acct-2",
                team_name="Team 2",
                current_members=1,
                max_members=6,
                status="active",
                pool_type="normal",
            )
            code = RedemptionCode(
                code="TEST-CODE-0001",
                status="unused",
                pool_type="normal",
                reusable_by_seat=False,
            )
            session.add_all([team_1, team_2, code])
            await session.commit()

    @staticmethod
    def _close_coro(coro):
        coro.close()
        return None

    @staticmethod
    async def _noop_async(*args, **kwargs):
        return None

    @staticmethod
    async def _return_token(*args, **kwargs):
        return "token"

    @staticmethod
    async def _stub_account_info_success(*args, **kwargs):
        return {
            "success": True,
            "accounts": [{
                "account_id": "acct-64",
                "name": "Floor Team",
                "plan_type": "team",
                "subscription_plan": "chatgpt-team",
                "has_active_subscription": True,
                "expires_at": None,
                "account_user_role": "account-owner",
            }],
        }

    @staticmethod
    async def _stub_members_four(*args, **kwargs):
        return {
            "success": True,
            "total": 4,
            "members": [
                {"email": "one@example.com"},
                {"email": "two@example.com"},
                {"email": "three@example.com"},
                {"email": "four@example.com"},
            ],
        }

    @staticmethod
    async def _stub_empty_invites(*args, **kwargs):
        return {
            "success": True,
            "total": 0,
            "items": [],
        }

    @staticmethod
    async def _stub_account_settings_success(*args, **kwargs):
        return {
            "success": True,
            "data": {"beta_settings": {}},
        }

    async def test_auto_select_skips_team_where_user_already_exists(self):
        await self._seed_basic_data()
        service = RedeemFlowService()
        service.redemption_service = StubRedemptionService()
        service.team_service = StubTeamService(
            active_team_ids_by_email={"user@example.com": [1]}
        )
        service.chatgpt_service = StubChatGPTService(
            {
                "acct-2": [{"success": True, "data": {"account_invites": [{"email": "user@example.com"}]}}],
            }
        )

        async with self.session_factory() as session:
            with patch("app.services.redeem_flow.asyncio.create_task", side_effect=self._close_coro):
                result = await service.redeem_and_join_team(
                    email="user@example.com",
                    code="TEST-CODE-0001",
                    team_id=None,
                    db_session=session,
                )

            self.assertTrue(result["success"])
            self.assertEqual(result["team_info"]["id"], 2)

            code = await session.get(RedemptionCode, 1)
            self.assertEqual(code.status, "used")
            self.assertEqual(code.used_team_id, 2)

            records = (await session.execute(select(RedemptionRecord))).scalars().all()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].team_id, 2)

    async def test_sync_reconcile_requires_three_misses_before_removed(self):
        await self._seed_basic_data()
        team_service = TeamService.__new__(TeamService)

        async with self.session_factory() as session:
            await team_service.upsert_team_email_mapping(
                team_id=1,
                email="user@example.com",
                status="joined",
                db_session=session,
                source="sync",
            )
            await session.commit()

            for expected_missing_count in (1, 2):
                await team_service._reconcile_team_email_mappings(1, set(), set(), session)
                await session.commit()

                mapping = (
                    await session.execute(
                        select(TeamEmailMapping).where(
                            TeamEmailMapping.team_id == 1,
                            TeamEmailMapping.email == "user@example.com",
                        )
                    )
                ).scalar_one()
                self.assertEqual(mapping.status, "joined")
                self.assertEqual(mapping.missing_sync_count, expected_missing_count)

            await team_service._reconcile_team_email_mappings(1, set(), set(), session)
            await session.commit()

            mapping = (
                await session.execute(
                    select(TeamEmailMapping).where(
                        TeamEmailMapping.team_id == 1,
                        TeamEmailMapping.email == "user@example.com",
                    )
                )
            ).scalar_one()
            self.assertEqual(mapping.status, "removed")
            self.assertEqual(mapping.missing_sync_count, 3)

    async def test_sync_reconcile_resets_missing_counter_when_email_returns(self):
        await self._seed_basic_data()
        team_service = TeamService.__new__(TeamService)

        async with self.session_factory() as session:
            await team_service.upsert_team_email_mapping(
                team_id=1,
                email="user@example.com",
                status="joined",
                db_session=session,
                source="sync",
            )
            await session.commit()

            await team_service._reconcile_team_email_mappings(1, set(), set(), session)
            await session.commit()

            await team_service._reconcile_team_email_mappings(1, {"user@example.com"}, set(), session)
            await session.commit()

            mapping = (
                await session.execute(
                    select(TeamEmailMapping).where(
                        TeamEmailMapping.team_id == 1,
                        TeamEmailMapping.email == "user@example.com",
                    )
                )
            ).scalar_one()
            self.assertEqual(mapping.status, "joined")
            self.assertEqual(mapping.missing_sync_count, 0)


    async def test_virtual_welfare_code_creates_shadow_code_for_redemption_record(self):
        async with self.session_factory() as session:
            team = Team(
                id=10,
                email="welfare-owner@example.com",
                access_token_encrypted="token-10",
                account_id="acct-welfare",
                team_name="Welfare Team",
                current_members=1,
                max_members=6,
                status="active",
                pool_type="welfare",
            )
            session.add(team)
            await session.commit()

            service = RedeemFlowService()
            shadow = await service.redemption_service.ensure_virtual_welfare_shadow_code(session, "WELF-TEST-CODE")
            await session.commit()

            self.assertIsNotNone(shadow)
            self.assertEqual(shadow.code, "WELF-TEST-CODE")
            self.assertEqual(shadow.pool_type, "welfare")
            self.assertTrue(shadow.reusable_by_seat)

            record = RedemptionRecord(
                email="user@example.com",
                code="WELF-TEST-CODE",
                team_id=10,
                account_id="acct-welfare",
            )
            session.add(record)
            await session.commit()

            stored_record = (await session.execute(select(RedemptionRecord).where(RedemptionRecord.code == "WELF-TEST-CODE"))).scalar_one()
            self.assertEqual(stored_record.team_id, 10)


    async def test_delete_used_normal_code_with_history_is_blocked(self):
        async with self.session_factory() as session:
            team = Team(
                id=20,
                email="normal-owner@example.com",
                access_token_encrypted="token-20",
                account_id="acct-normal",
                team_name="Normal Team",
                current_members=2,
                max_members=6,
                status="active",
                pool_type="normal",
            )
            code = RedemptionCode(
                code="NORMAL-CODE-DELETE",
                status="used",
                used_by_email="user@example.com",
                used_team_id=20,
                pool_type="normal",
            )
            session.add(team)
            await session.commit()

            session.add(code)
            await session.commit()

            session.add(
                RedemptionRecord(
                    email="user@example.com",
                    code="NORMAL-CODE-DELETE",
                    team_id=20,
                    account_id="acct-normal",
                )
            )
            await session.commit()

            service = RedemptionService()
            result = await service.delete_code("NORMAL-CODE-DELETE", session)

            self.assertFalse(result["success"])
            self.assertIn("无法直接删除", result["error"])

            remaining_code = (
                await session.execute(
                    select(RedemptionCode).where(RedemptionCode.code == "NORMAL-CODE-DELETE")
                )
            ).scalar_one_or_none()
            self.assertIsNotNone(remaining_code)

    async def test_atomic_seat_reservation_prevents_over_allocation(self):
        async with self.session_factory() as session:
            team = Team(
                id=30,
                email="capacity-owner@example.com",
                access_token_encrypted="token-30",
                account_id="acct-capacity",
                team_name="Capacity Team",
                current_members=5,
                max_members=6,
                status="active",
                pool_type="normal",
            )
            session.add(team)
            await session.commit()

        async with self.session_factory() as session_one, self.session_factory() as session_two:
            team_service = TeamService()
            reserve_one, reserve_two = await asyncio.gather(
                team_service.reserve_seat_if_available(30, session_one, pool_type="normal"),
                team_service.reserve_seat_if_available(30, session_two, pool_type="normal"),
            )

            successes = [result for result in (reserve_one, reserve_two) if result["success"]]
            failures = [result for result in (reserve_one, reserve_two) if not result["success"]]

            self.assertEqual(len(successes), 1)
            self.assertEqual(len(failures), 1)
            self.assertIn("已满", failures[0]["error"])

            await session_one.commit()
            await session_two.rollback()

        async with self.session_factory() as verify_session:
            stored_team = await verify_session.get(Team, 30)
            self.assertIsNotNone(stored_team)
            self.assertEqual(stored_team.current_members, 6)
            self.assertEqual(stored_team.status, "full")

    async def test_locked_team_returns_conflict_without_consuming_code(self):
        await self._seed_basic_data()
        service = RedeemFlowService()
        service.redemption_service = StubRedemptionService()
        service.team_service = StubTeamService(
            active_team_ids_by_email={"user@example.com": [1]}
        )
        service.chatgpt_service = StubChatGPTService({})

        async with self.session_factory() as session:
            with patch("app.services.redeem_flow.asyncio.create_task", side_effect=self._close_coro):
                result = await service.redeem_and_join_team(
                    email="user@example.com",
                    code="TEST-CODE-0001",
                    team_id=1,
                    db_session=session,
                )

            self.assertFalse(result["success"])
            self.assertIn("当前兑换码不会被消耗", result["error"])

            code = await session.get(RedemptionCode, 1)
            self.assertEqual(code.status, "unused")
            self.assertIsNone(code.used_team_id)

            records = (await session.execute(select(RedemptionRecord))).scalars().all()
            self.assertEqual(records, [])

            team_1 = await session.get(Team, 1)
            self.assertEqual(team_1.current_members, 3)

    async def test_auto_retry_when_invite_api_reports_user_already_in_team(self):
        await self._seed_basic_data()
        service = RedeemFlowService()
        service.redemption_service = StubRedemptionService()
        service.team_service = StubTeamService()
        service.chatgpt_service = StubChatGPTService(
            {
                "acct-1": [{"success": False, "error": "Already in workspace"}],
                "acct-2": [{"success": True, "data": {"account_invites": [{"email": "user@example.com"}]}}],
            }
        )

        async with self.session_factory() as session:
            with patch("app.services.redeem_flow.asyncio.create_task", side_effect=self._close_coro):
                result = await service.redeem_and_join_team(
                    email="user@example.com",
                    code="TEST-CODE-0001",
                    team_id=None,
                    db_session=session,
                )

            self.assertTrue(result["success"])
            self.assertEqual(result["team_info"]["id"], 2)

            code = await session.get(RedemptionCode, 1)
            self.assertEqual(code.used_team_id, 2)

            records = (await session.execute(select(RedemptionRecord))).scalars().all()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].team_id, 2)

    async def test_auto_retry_when_invite_api_reports_team_banned(self):
        await self._seed_basic_data()
        service = RedeemFlowService()
        service.redemption_service = StubRedemptionService()
        service.team_service = StubTeamService()
        service.chatgpt_service = StubChatGPTService(
            {
                "acct-1": [{"success": False, "error": "account deactivated", "error_code": "account_deactivated"}],
                "acct-2": [{"success": True, "data": {"account_invites": [{"email": "user@example.com"}]}}],
            }
        )

        async with self.session_factory() as session:
            with patch("app.services.redeem_flow.asyncio.create_task", side_effect=self._close_coro):
                result = await service.redeem_and_join_team(
                    email="user@example.com",
                    code="TEST-CODE-0001",
                    team_id=None,
                    db_session=session,
                )

            self.assertTrue(result["success"])
            self.assertEqual(result["team_info"]["id"], 2)

            team_1 = await session.get(Team, 1)
            self.assertEqual(team_1.status, "banned")
            self.assertEqual(team_1.current_members, 3)

            code = await session.get(RedemptionCode, 1)
            self.assertEqual(code.status, "used")
            self.assertEqual(code.used_team_id, 2)

            records = (await session.execute(select(RedemptionRecord))).scalars().all()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].team_id, 2)

    async def test_auto_retry_when_invite_api_reports_token_invalidated(self):
        await self._seed_basic_data()
        service = RedeemFlowService()
        service.redemption_service = StubRedemptionService()
        service.team_service = StubTeamService()
        service.chatgpt_service = StubChatGPTService(
            {
                "acct-1": [{"success": False, "error": "token has been invalidated", "error_code": "token_invalidated"}],
                "acct-2": [{"success": True, "data": {"account_invites": [{"email": "user@example.com"}]}}],
            }
        )

        async with self.session_factory() as session:
            with patch("app.services.redeem_flow.asyncio.create_task", side_effect=self._close_coro):
                result = await service.redeem_and_join_team(
                    email="user@example.com",
                    code="TEST-CODE-0001",
                    team_id=None,
                    db_session=session,
                )

            self.assertTrue(result["success"])
            self.assertEqual(result["team_info"]["id"], 2)

            team_1 = await session.get(Team, 1)
            self.assertEqual(team_1.status, "banned")
            self.assertEqual(team_1.current_members, 3)

            code = await session.get(RedemptionCode, 1)
            self.assertEqual(code.status, "used")
            self.assertEqual(code.used_team_id, 2)

    async def test_locked_team_banned_returns_conflict_without_consuming_code(self):
        await self._seed_basic_data()
        service = RedeemFlowService()
        service.redemption_service = StubRedemptionService()
        service.team_service = StubTeamService()
        service.chatgpt_service = StubChatGPTService(
            {
                "acct-1": [{"success": False, "error": "account deactivated", "error_code": "account_deactivated"}],
            }
        )

        async with self.session_factory() as session:
            with patch("app.services.redeem_flow.asyncio.create_task", side_effect=self._close_coro):
                result = await service.redeem_and_join_team(
                    email="user@example.com",
                    code="TEST-CODE-0001",
                    team_id=1,
                    db_session=session,
                )

            self.assertFalse(result["success"])
            self.assertIn("登录状态已失效", result["error"])

            team_1 = await session.get(Team, 1)
            self.assertEqual(team_1.status, "banned")
            self.assertEqual(team_1.current_members, 3)

            code = await session.get(RedemptionCode, 1)
            self.assertEqual(code.status, "unused")
            self.assertIsNone(code.used_team_id)

            records = (await session.execute(select(RedemptionRecord))).scalars().all()
            self.assertEqual(records, [])

    async def test_validate_code_rejects_expired_warranty_code(self):
        async with self.session_factory() as session:
            code = RedemptionCode(
                code="WARRANTY-EXPIRED-001",
                status="used",
                has_warranty=True,
                warranty_days=30,
                used_at=get_now() - timedelta(days=40),
                warranty_expires_at=get_now() - timedelta(days=10),
            )
            session.add(code)
            await session.commit()

            service = RedemptionService()
            result = await service.validate_code("WARRANTY-EXPIRED-001", session)

            self.assertTrue(result["success"])
            self.assertFalse(result["valid"])
            self.assertEqual(result["reason"], "质保已过期")

            refreshed_code = (
                await session.execute(
                    select(RedemptionCode).where(RedemptionCode.code == "WARRANTY-EXPIRED-001")
                )
            ).scalar_one()
            self.assertEqual(refreshed_code.status, "expired")

    async def test_warranty_reuse_rejects_email_handoff(self):
        async with self.session_factory() as session:
            team = Team(
                id=40,
                email="owner-40@example.com",
                access_token_encrypted="token-40",
                account_id="acct-40",
                team_name="Old Team",
                current_members=6,
                max_members=6,
                status="expired",
                pool_type="normal",
            )
            code = RedemptionCode(
                code="WARRANTY-HANDOFF-001",
                status="used",
                has_warranty=True,
                warranty_days=30,
                used_at=get_now() - timedelta(days=3),
                warranty_expires_at=get_now() + timedelta(days=27),
            )
            record = RedemptionRecord(
                email="buyer@example.com",
                code="WARRANTY-HANDOFF-001",
                team_id=40,
                account_id="acct-40",
                is_warranty_redemption=False,
            )
            session.add_all([team, code, record])
            await session.commit()

            service = WarrantyService()
            result = await service.validate_warranty_reuse(
                session,
                "WARRANTY-HANDOFF-001",
                "attacker@example.com",
            )

            self.assertTrue(result["success"])
            self.assertFalse(result["can_reuse"])
            self.assertIn("仅限原使用邮箱", result["reason"])

    async def test_warranty_check_keeps_record_when_sync_misses_member(self):
        async with self.session_factory() as session:
            team = Team(
                id=50,
                email="owner-50@example.com",
                access_token_encrypted="token-50",
                account_id="acct-50",
                team_name="Sync Team",
                current_members=2,
                max_members=6,
                status="active",
                pool_type="normal",
            )
            code = RedemptionCode(
                code="WARRANTY-CHECK-001",
                status="used",
                has_warranty=True,
                warranty_days=30,
                used_at=get_now() - timedelta(days=1),
                warranty_expires_at=get_now() + timedelta(days=29),
            )
            record = RedemptionRecord(
                email="buyer@example.com",
                code="WARRANTY-CHECK-001",
                team_id=50,
                account_id="acct-50",
            )
            session.add_all([team, code, record])
            await session.commit()

            service = WarrantyService()
            service.team_service = StubTeamService(sync_results={50: [{"success": True, "member_emails": []}]})

            result = await service.check_warranty_status(
                session,
                code="WARRANTY-CHECK-001",
            )

            self.assertTrue(result["success"])
            self.assertTrue(result["has_warranty"])
            self.assertEqual(len(result["records"]), 1)
            self.assertEqual(result["records"][0]["team_status"], "suspected_inconsistent")
            self.assertIn("保留原始记录", result["message"])

            stored_records = (
                await session.execute(
                    select(RedemptionRecord).where(RedemptionRecord.code == "WARRANTY-CHECK-001")
                )
            ).scalars().all()
            self.assertEqual(len(stored_records), 1)

    async def test_warranty_reuse_allows_original_email_after_orphan_cleanup(self):
        async with self.session_factory() as session:
            team = Team(
                id=51,
                email="owner-51@example.com",
                access_token_encrypted="token-51",
                account_id="acct-51",
                team_name="Ghost Team",
                current_members=2,
                max_members=6,
                status="active",
                pool_type="normal",
            )
            code = RedemptionCode(
                code="WARRANTY-GHOST-001",
                status="used",
                has_warranty=True,
                warranty_days=30,
                used_at=get_now() - timedelta(days=2),
                warranty_expires_at=get_now() + timedelta(days=28),
            )
            record = RedemptionRecord(
                email="buyer@example.com",
                code="WARRANTY-GHOST-001",
                team_id=51,
                account_id="acct-51",
            )
            session.add_all([team, code, record])
            await session.commit()

            service = WarrantyService()
            service.team_service = StubTeamService(sync_results={51: [{"success": True, "member_emails": []}]})

            result = await service.validate_warranty_reuse(
                session,
                "WARRANTY-GHOST-001",
                "buyer@example.com",
            )

            self.assertTrue(result["success"])
            self.assertTrue(result["can_reuse"])
            self.assertIn("已自动修复", result["reason"])

            stored_records = (
                await session.execute(
                    select(RedemptionRecord).where(RedemptionRecord.code == "WARRANTY-GHOST-001")
                )
            ).scalars().all()
            self.assertEqual(stored_records, [])

    async def test_seat_rolls_back_after_full_error(self):
        await self._seed_basic_data()
        service = RedeemFlowService()
        service.redemption_service = StubRedemptionService()
        service.team_service = StubTeamService()
        service.chatgpt_service = StubChatGPTService(
            {
                "acct-1": [{"success": False, "error": "maximum number of seats reached"}],
            }
        )

        async with self.session_factory() as session:
            team_1 = await session.get(Team, 1)
            team_1.current_members = 5
            team_1.max_members = 6
            await session.commit()

            with patch("app.services.redeem_flow.asyncio.create_task", side_effect=self._close_coro):
                result = await service.redeem_and_join_team(
                    email="user@example.com",
                    code="TEST-CODE-0001",
                    team_id=1,
                    db_session=session,
                )

            self.assertFalse(result["success"])
            self.assertIn("席位已满", result["error"])

            refreshed_team = await session.get(Team, 1)
            self.assertEqual(refreshed_team.current_members, 5)
            self.assertEqual(refreshed_team.status, "active")

    async def test_virtual_welfare_code_usage_does_not_double_decrement_remaining(self):
        async with self.session_factory() as session:
            welfare_team = Team(
                id=60,
                email="welfare-owner@example.com",
                access_token_encrypted="token-60",
                account_id="acct-60",
                team_name="Welfare Pool",
                current_members=2,
                max_members=5,
                status="active",
                pool_type="welfare",
            )
            session.add(welfare_team)
            await session.commit()

            service = RedemptionService()
            await service.ensure_virtual_welfare_shadow_code(session, "WELF-CODE-001")
            settings_service.clear_cache()
            await settings_service.update_setting(session, "welfare_common_code", "WELF-CODE-001")
            await settings_service.update_setting(session, "welfare_common_code_team_id", "60")
            session.add_all([
                RedemptionRecord(
                    email="one@example.com",
                    code="WELF-CODE-001",
                    team_id=60,
                    account_id="acct-60",
                ),
                RedemptionRecord(
                    email="two@example.com",
                    code="WELF-CODE-001",
                    team_id=60,
                    account_id="acct-60",
                ),
            ])
            await session.commit()

            await settings_service.update_setting(session, "welfare_common_code_limit", "5")
            await settings_service.update_setting(session, "welfare_common_code_used_count", "2")

            usage = await service.get_virtual_welfare_code_usage(session, welfare_code="WELF-CODE-001")
            self.assertEqual(usage["used_count"], 2)
            self.assertEqual(usage["configured_limit"], 5)
            self.assertEqual(usage["usable_capacity"], 3)
            self.assertEqual(usage["remaining_count"], 3)

            result = await service.validate_code("WELF-CODE-001", session)
            self.assertTrue(result["success"])
            self.assertTrue(result["valid"])
            self.assertEqual(result["redemption_code"]["limit"], 5)
            self.assertEqual(result["redemption_code"]["used_count"], 2)
            self.assertEqual(result["redemption_code"]["remaining"], 3)

    async def test_virtual_welfare_code_handles_concurrent_redemptions_up_to_capacity(self):
        async with self.session_factory() as session:
            welfare_team = Team(
                id=61,
                email="welfare-owner-61@example.com",
                access_token_encrypted="token-61",
                account_id="acct-61",
                team_name="Welfare Concurrent Team",
                current_members=0,
                max_members=10,
                status="active",
                pool_type="welfare",
            )
            session.add_all([
                welfare_team,
            ])
            await session.commit()
            settings_service.clear_cache()
            await settings_service.update_setting(session, "welfare_common_code", "WELF-CONCURRENT-001")
            await settings_service.update_setting(session, "welfare_common_code_limit", "5")
            await settings_service.update_setting(session, "welfare_common_code_used_count", "0")
            await settings_service.update_setting(session, "welfare_common_code_team_id", "61")

            service = RedeemFlowService()
            service.team_service = StubTeamService()
            service.chatgpt_service = StubChatGPTService(
                {
                    "acct-61": [
                        {"success": True, "data": {"account_invites": [{"email": f"user{i}@example.com"}]}}
                        for i in range(6)
                    ]
                }
            )

            async def redeem(email):
                async with self.session_factory() as inner_session:
                    with patch.object(service, "_background_verify_sync", new=self._noop_async), \
                         patch.object(notification_service, "check_and_notify_low_stock", new=self._noop_async):
                        return await service.redeem_and_join_team(
                            email=email,
                            code="WELF-CONCURRENT-001",
                            team_id=None,
                            db_session=inner_session,
                        )

            results = await asyncio.gather(*[
                redeem(f"user{i}@example.com")
                for i in range(6)
            ])

            success_count = sum(1 for result in results if result["success"])
            failure_count = sum(1 for result in results if not result["success"])
            self.assertEqual(success_count, 5)
            self.assertEqual(failure_count, 1)

            async with self.session_factory() as verify_session:
                stored_team = await verify_session.get(Team, 61)
                self.assertEqual(stored_team.current_members, 5)
                self.assertEqual(stored_team.status, "active")

                records = (
                    await verify_session.execute(
                        select(RedemptionRecord).where(RedemptionRecord.code == "WELF-CONCURRENT-001")
                    )
                ).scalars().all()
                self.assertEqual(len(records), 5)

                usage = await RedemptionService().get_virtual_welfare_code_usage(
                    verify_session,
                    welfare_code="WELF-CONCURRENT-001",
                )
                self.assertEqual(usage["configured_limit"], 5)
                self.assertEqual(usage["remaining_count"], 0)

    async def test_virtual_welfare_code_stays_invalid_after_rotation_even_if_new_capacity_appears(self):
        async with self.session_factory() as session:
            old_team = Team(
                id=62,
                email="welfare-owner-62@example.com",
                access_token_encrypted="token-62",
                account_id="acct-62",
                team_name="Old Welfare Team",
                current_members=5,
                max_members=5,
                status="full",
                pool_type="welfare",
            )
            session.add(old_team)
            await session.commit()

            service = RedemptionService()
            await service.ensure_virtual_welfare_shadow_code(session, "OLD-WELF-CODE")
            settings_service.clear_cache()
            await settings_service.update_setting(session, "welfare_common_code", "OLD-WELF-CODE")
            await settings_service.update_setting(session, "welfare_common_code_limit", "5")
            await settings_service.update_setting(session, "welfare_common_code_used_count", "5")
            await settings_service.update_setting(session, "welfare_common_code_team_id", "62")
            await session.commit()

            old_result = await service.validate_code("OLD-WELF-CODE", session)
            self.assertTrue(old_result["success"])
            self.assertFalse(old_result["valid"])

            new_team = Team(
                id=63,
                email="welfare-owner-63@example.com",
                access_token_encrypted="token-63",
                account_id="acct-63",
                team_name="New Welfare Team",
                current_members=0,
                max_members=5,
                status="active",
                pool_type="welfare",
            )
            session.add(new_team)
            await session.commit()

            await service.ensure_virtual_welfare_shadow_code(session, "NEW-WELF-CODE")
            await settings_service.update_setting(session, "welfare_common_code", "NEW-WELF-CODE")
            await settings_service.update_setting(session, "welfare_common_code_limit", "5")
            await settings_service.update_setting(session, "welfare_common_code_used_count", "0")
            await settings_service.update_setting(session, "welfare_common_code_team_id", "63")
            await session.commit()

            stale_result = await service.validate_code("OLD-WELF-CODE", session)
            self.assertTrue(stale_result["success"])
            self.assertFalse(stale_result["valid"])
            self.assertIn("已失效", stale_result["reason"])

            fresh_result = await service.validate_code("NEW-WELF-CODE", session)
            self.assertTrue(fresh_result["success"])
            self.assertTrue(fresh_result["valid"])
            self.assertEqual(fresh_result["redemption_code"]["limit"], 5)
            self.assertEqual(fresh_result["redemption_code"]["remaining"], 5)

    async def test_virtual_welfare_code_limit_blocks_redemption_even_when_live_capacity_is_higher(self):
        async with self.session_factory() as session:
            welfare_team = Team(
                id=65,
                email="welfare-owner-65@example.com",
                access_token_encrypted="token-65",
                account_id="acct-65",
                team_name="Large Welfare Team",
                current_members=0,
                max_members=10,
                status="active",
                pool_type="welfare",
            )
            session.add(welfare_team)
            await session.commit()

            service = RedemptionService()
            await service.ensure_virtual_welfare_shadow_code(session, "WELF-LIMIT-001")
            settings_service.clear_cache()
            await settings_service.update_setting(session, "welfare_common_code", "WELF-LIMIT-001")
            await settings_service.update_setting(session, "welfare_common_code_limit", "5")
            await settings_service.update_setting(session, "welfare_common_code_used_count", "5")
            await settings_service.update_setting(session, "welfare_common_code_team_id", "65")
            await session.commit()

            result = await service.validate_code("WELF-LIMIT-001", session)
            self.assertTrue(result["success"])
            self.assertFalse(result["valid"])
            self.assertIn("次数已用完", result["reason"])

    async def test_sync_team_info_keeps_local_member_floor_when_remote_count_is_stale(self):
        async with self.session_factory() as session:
            team = Team(
                id=64,
                email="welfare-owner-64@example.com",
                access_token_encrypted="token-64",
                account_id="acct-64",
                team_name="Floor Team",
                current_members=5,
                max_members=5,
                status="full",
                pool_type="welfare",
            )
            session.add(team)
            await session.commit()

            team_service = TeamService()
            await team_service.upsert_team_email_mapping(
                team_id=64,
                email="one@example.com",
                status="joined",
                db_session=session,
                source="sync",
            )
            await team_service.upsert_team_email_mapping(
                team_id=64,
                email="two@example.com",
                status="joined",
                db_session=session,
                source="sync",
            )
            await team_service.upsert_team_email_mapping(
                team_id=64,
                email="three@example.com",
                status="joined",
                db_session=session,
                source="sync",
            )
            await team_service.upsert_team_email_mapping(
                team_id=64,
                email="four@example.com",
                status="joined",
                db_session=session,
                source="sync",
            )
            await team_service.upsert_team_email_mapping(
                team_id=64,
                email="five@example.com",
                status="invited",
                db_session=session,
                source="redeem",
            )
            await session.commit()

            with patch.object(team_service, "ensure_access_token", new=self._return_token), \
                 patch.object(team_service.chatgpt_service, "get_account_info", new=self._stub_account_info_success), \
                 patch.object(team_service.chatgpt_service, "get_members", new=self._stub_members_four), \
                 patch.object(team_service.chatgpt_service, "get_invites", new=self._stub_empty_invites), \
                 patch.object(team_service.chatgpt_service, "get_account_settings", new=self._stub_account_settings_success):
                result = await team_service.sync_team_info(64, session)

            self.assertTrue(result["success"])
            self.assertEqual(result["message"], "同步成功,当前成员数: 5")

            self.assertTrue(result["success"])
            refreshed_team = await session.get(Team, 64)
            self.assertEqual(refreshed_team.current_members, 5)
            self.assertEqual(refreshed_team.status, "full")

    async def test_apply_member_count_floor_allows_count_to_fall_after_mapping_removed(self):
        async with self.session_factory() as session:
            team = Team(
                id=66,
                email="welfare-owner-66@example.com",
                access_token_encrypted="token-66",
                account_id="acct-66",
                team_name="Falling Team",
                current_members=3,
                max_members=5,
                status="active",
                pool_type="welfare",
            )
            session.add(team)
            await session.commit()

            team_service = TeamService()
            await team_service.upsert_team_email_mapping(66, "one@example.com", "joined", session, source="sync")
            await team_service.upsert_team_email_mapping(66, "two@example.com", "joined", session, source="sync")
            await team_service.mark_team_email_mapping_removed(66, "three@example.com", session, source="api")
            await session.commit()

            effective_members = await team_service._apply_member_count_floor(team, 2, session)
            await session.commit()

            self.assertEqual(effective_members, 2)
            refreshed_team = await session.get(Team, 66)
            self.assertEqual(refreshed_team.current_members, 2)
            self.assertEqual(refreshed_team.status, "active")

    async def test_get_team_members_filters_non_pending_invites_and_joined_duplicates(self):
        async with self.session_factory() as session:
            team = Team(
                id=67,
                email="welfare-owner-67@example.com",
                access_token_encrypted="token-67",
                account_id="acct-67",
                team_name="Invite Filter Team",
                current_members=3,
                max_members=6,
                status="active",
                pool_type="welfare",
            )
            session.add(team)
            await session.commit()

            team_service = TeamService()

            async def stub_members(*args, **kwargs):
                return {
                    "success": True,
                    "total": 2,
                    "members": [
                        {"id": "u1", "email": "joined@example.com", "role": "standard-user", "created_time": "2026-04-16T08:00:00"},
                        {"id": "u2", "email": "member2@example.com", "role": "standard-user", "created_time": "2026-04-16T08:05:00"},
                    ],
                }

            async def stub_invites(*args, **kwargs):
                return {
                    "success": True,
                    "total": 3,
                    "items": [
                        {"email_address": "joined@example.com", "role": "standard-user", "created_time": "2026-04-16T08:10:00", "status": 2, "state": "pending"},
                        {"email_address": "pending@example.com", "role": "standard-user", "created_time": "2026-04-16T08:15:00", "status": 2, "state": "pending"},
                        {"email_address": "accepted@example.com", "role": "standard-user", "created_time": "2026-04-16T08:20:00", "status": "accepted", "state": "accepted"},
                    ],
                }

            with patch.object(team_service, "ensure_access_token", new=self._return_token), \
                 patch.object(team_service.chatgpt_service, "get_members", new=stub_members), \
                 patch.object(team_service.chatgpt_service, "get_invites", new=stub_invites):
                result = await team_service.get_team_members(67, session)

            self.assertTrue(result["success"])
            self.assertEqual(result["total"], 3)
            emails = [item["email"] for item in result["members"]]
            self.assertEqual(emails.count("joined@example.com"), 1)
            self.assertIn("pending@example.com", emails)
            self.assertNotIn("accepted@example.com", emails)

    async def test_filter_pending_invites_accepts_numeric_status_two(self):
        team_service = TeamService()

        pending_invites = team_service._filter_pending_invites(
            [
                {"email_address": "numeric@example.com", "status": 2},
                {"email_address": "string@example.com", "status": "pending"},
                {"email_address": "accepted@example.com", "status": "accepted"},
            ],
            joined_emails=set(),
        )

        emails = [item["email_address"] for item in pending_invites]
        self.assertIn("numeric@example.com", emails)
        self.assertIn("string@example.com", emails)
        self.assertNotIn("accepted@example.com", emails)

    async def test_filter_pending_invites_prefers_state_over_numeric_status(self):
        team_service = TeamService()

        pending_invites = team_service._filter_pending_invites(
            [
                {"email_address": "state-pending@example.com", "status": 2, "state": "pending"},
                {"email_address": "state-accepted@example.com", "status": 2, "state": "accepted"},
            ],
            joined_emails=set(),
        )

        emails = [item["email_address"] for item in pending_invites]
        self.assertIn("state-pending@example.com", emails)
        self.assertNotIn("state-accepted@example.com", emails)

    async def test_generate_welfare_common_code_binds_selected_team(self):
        async with self.session_factory() as session:
            welfare_team = Team(
                id=68,
                email="welfare-owner-68@example.com",
                access_token_encrypted="token-68",
                account_id="acct-68",
                team_name="Bound Welfare Team",
                current_members=3,
                max_members=5,
                status="active",
                pool_type="welfare",
            )
            other_team = Team(
                id=69,
                email="welfare-owner-69@example.com",
                access_token_encrypted="token-69",
                account_id="acct-69",
                team_name="Other Welfare Team",
                current_members=0,
                max_members=10,
                status="active",
                pool_type="welfare",
            )
            session.add_all([welfare_team, other_team])
            await session.commit()

            response = await generate_welfare_common_code(
                payload=WelfareCodeGenerateRequest(team_id=68),
                db=session,
                current_user={"username": "admin"},
            )

            self.assertIsInstance(response, JSONResponse)
            payload = json.loads(response.body.decode("utf-8"))
            self.assertTrue(payload["success"])
            self.assertEqual(payload["team_id"], 68)
            self.assertEqual(payload["limit"], 2)
            self.assertEqual(payload["remaining"], 2)

            stored_team_id = await settings_service.get_setting(session, "welfare_common_code_team_id", "")
            self.assertEqual(stored_team_id, "68")

            code_value = payload["code"]
            usage = await RedemptionService().get_virtual_welfare_code_usage(session, welfare_code=code_value)
            self.assertEqual(usage["team_id"], 68)
            self.assertEqual(usage["configured_limit"], 2)
            self.assertEqual(usage["remaining_count"], 2)
            self.assertEqual(usage["usable_capacity"], 2)

    async def test_generate_welfare_common_code_returns_error_when_settings_write_fails(self):
        async with self.session_factory() as session:
            welfare_team = Team(
                id=75,
                email="welfare-owner-75@example.com",
                access_token_encrypted="token-75",
                account_id="acct-75",
                team_name="Settings Failure Team",
                current_members=1,
                max_members=4,
                status="active",
                pool_type="welfare",
            )
            session.add(welfare_team)
            await session.commit()

            with patch("app.routes.admin.settings_service.update_settings", return_value=False):
                response = await generate_welfare_common_code(
                    payload=WelfareCodeGenerateRequest(team_id=75),
                    db=session,
                    current_user={"username": "admin"},
                )

            self.assertIsInstance(response, JSONResponse)
            self.assertEqual(response.status_code, 500)
            payload = json.loads(response.body.decode("utf-8"))
            self.assertFalse(payload["success"])
            self.assertIn("写入福利通用兑换码配置失败", payload["error"])

    async def test_verify_virtual_welfare_code_returns_only_bound_team(self):
        async with self.session_factory() as session:
            bound_team = Team(
                id=70,
                email="welfare-owner-70@example.com",
                access_token_encrypted="token-70",
                account_id="acct-70",
                team_name="Bound Team",
                current_members=1,
                max_members=4,
                status="active",
                pool_type="welfare",
            )
            other_team = Team(
                id=71,
                email="welfare-owner-71@example.com",
                access_token_encrypted="token-71",
                account_id="acct-71",
                team_name="Other Team",
                current_members=0,
                max_members=6,
                status="active",
                pool_type="welfare",
            )
            session.add_all([bound_team, other_team])
            await session.commit()

            service = RedemptionService()
            await service.ensure_virtual_welfare_shadow_code(session, "WELF-BOUND-001")
            settings_service.clear_cache()
            await settings_service.update_setting(session, "welfare_common_code", "WELF-BOUND-001")
            await settings_service.update_setting(session, "welfare_common_code_limit", "3")
            await settings_service.update_setting(session, "welfare_common_code_used_count", "0")
            await settings_service.update_setting(session, "welfare_common_code_team_id", "70")
            await session.commit()

            flow_service = RedeemFlowService()
            result = await flow_service.verify_code_and_get_teams("WELF-BOUND-001", session)

            self.assertTrue(result["success"])
            self.assertTrue(result["valid"])
            self.assertEqual(len(result["teams"]), 1)
            self.assertEqual(result["teams"][0]["id"], 70)

    async def test_redeem_virtual_welfare_code_rejects_unbound_selected_team(self):
        async with self.session_factory() as session:
            bound_team = Team(
                id=72,
                email="welfare-owner-72@example.com",
                access_token_encrypted="token-72",
                account_id="acct-72",
                team_name="Bound Redeem Team",
                current_members=0,
                max_members=3,
                status="active",
                pool_type="welfare",
            )
            other_team = Team(
                id=73,
                email="welfare-owner-73@example.com",
                access_token_encrypted="token-73",
                account_id="acct-73",
                team_name="Other Redeem Team",
                current_members=0,
                max_members=3,
                status="active",
                pool_type="welfare",
            )
            session.add_all([bound_team, other_team])
            await session.commit()

            redemption_service = RedemptionService()
            await redemption_service.ensure_virtual_welfare_shadow_code(session, "WELF-BOUND-REDEEM")
            settings_service.clear_cache()
            await settings_service.update_setting(session, "welfare_common_code", "WELF-BOUND-REDEEM")
            await settings_service.update_setting(session, "welfare_common_code_limit", "3")
            await settings_service.update_setting(session, "welfare_common_code_used_count", "0")
            await settings_service.update_setting(session, "welfare_common_code_team_id", "72")
            await session.commit()

            service = RedeemFlowService()
            service.chatgpt_service = StubChatGPTService({})

            with patch("app.services.redeem_flow.asyncio.create_task", side_effect=self._close_coro):
                result = await service.redeem_and_join_team(
                    email="user@example.com",
                    code="WELF-BOUND-REDEEM",
                    team_id=73,
                    db_session=session,
                )

            self.assertFalse(result["success"])
            self.assertIn("仅可兑换到其绑定的 Team", result["error"])

    async def test_validate_virtual_welfare_code_fails_when_team_binding_points_to_deleted_team(self):
        async with self.session_factory() as session:
            team = Team(
                id=74,
                email="welfare-owner-74@example.com",
                access_token_encrypted="token-74",
                account_id="acct-74",
                team_name="Deleted Binding Team",
                current_members=0,
                max_members=4,
                status="active",
                pool_type="welfare",
            )
            session.add(team)
            await session.commit()

            service = RedemptionService()
            await service.ensure_virtual_welfare_shadow_code(session, "WELF-DELETED-BIND")
            settings_service.clear_cache()
            await settings_service.update_setting(session, "welfare_common_code", "WELF-DELETED-BIND")
            await settings_service.update_setting(session, "welfare_common_code_limit", "4")
            await settings_service.update_setting(session, "welfare_common_code_used_count", "0")
            await settings_service.update_setting(session, "welfare_common_code_team_id", "74")
            await session.delete(team)
            await session.commit()

            result = await service.validate_code("WELF-DELETED-BIND", session)
            self.assertTrue(result["success"])
            self.assertFalse(result["valid"])
            self.assertIn("未绑定有效 Team", result["reason"])
