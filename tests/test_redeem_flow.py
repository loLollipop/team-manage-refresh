import unittest
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import RedemptionCode, RedemptionRecord, Team, TeamEmailMapping
from app.services.redeem_flow import RedeemFlowService
from app.services.redemption import RedemptionService
from app.services.team import TeamService


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
    def __init__(self, sync_results=None, active_team_ids_by_email=None):
        self.sync_results = sync_results or {}
        self.active_team_ids_by_email = {
            str(email).strip().lower(): set(team_ids)
            for email, team_ids in (active_team_ids_by_email or {}).items()
        }
        self.mapping_updates = []

    async def sync_team_info(self, team_id, db_session):
        team_results = (self.sync_results or {}).get(team_id, [])
        if team_results:
            result = team_results.pop(0)
            if team_results:
                return result
            self.sync_results[team_id] = [result]
            return result

        return {"success": True, "member_emails": [], "error": None}

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
