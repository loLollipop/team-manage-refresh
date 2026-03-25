import json
import unittest

import jwt

from app.models import Team
from app.services.sub2api import Sub2apiService
from app.services.team import TeamService


class TeamJsonImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_team_json_supports_sub2api_accounts_payload(self):
        service = TeamService.__new__(TeamService)
        imported_items = []

        async def fake_import_team_single(
            self,
            access_token,
            db_session,
            email=None,
            account_id=None,
            id_token=None,
            refresh_token=None,
            session_token=None,
            client_id=None,
            pool_type="normal",
        ):
            imported_items.append(
                {
                    "access_token": access_token,
                    "email": email,
                    "account_id": account_id,
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "pool_type": pool_type,
                }
            )
            return {
                "success": True,
                "team_id": len(imported_items),
                "email": email,
                "message": "ok",
                "error": None,
            }

        service.import_team_single = fake_import_team_single.__get__(service, TeamService)

        payload = {
            "type": "sub2api-data",
            "version": 1,
            "accounts": [
                {
                    "name": "sub2api-owner@example.com",
                    "platform": "openai",
                    "type": "oauth",
                    "credentials": {
                        "access_token": "at-1",
                        "refresh_token": "rt-1",
                        "client_id": "app_test_client",
                        "chatgpt_account_id": "acct-1",
                    },
                    "extra": {
                        "email": "sub2api-owner@example.com",
                    },
                }
            ],
        }

        results = [
            item async for item in service.import_team_json(
                json_text=json.dumps(payload),
                db_session=None,
                pool_type="welfare",
            )
        ]

        self.assertEqual(results[0]["type"], "start")
        self.assertEqual(results[0]["total"], 1)
        self.assertEqual(results[-1]["type"], "finish")
        self.assertEqual(results[-1]["success_count"], 1)
        self.assertEqual(len(imported_items), 1)
        self.assertEqual(imported_items[0]["email"], "sub2api-owner@example.com")
        self.assertEqual(imported_items[0]["account_id"], "acct-1")
        self.assertEqual(imported_items[0]["client_id"], "app_test_client")
        self.assertEqual(imported_items[0]["pool_type"], "welfare")


class Sub2apiServiceTests(unittest.TestCase):
    def test_build_request_headers_uses_x_api_key(self):
        headers = Sub2apiService._build_request_headers("admin-test-key")

        self.assertEqual(headers["x-api-key"], "admin-test-key")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertNotIn("Authorization", headers)

    def test_build_account_payload_uses_team_and_token_fields(self):
        service = Sub2apiService()
        access_token = jwt.encode(
            {
                "exp": 1893456000,
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "acct-from-token",
                    "chatgpt_user_id": "user-from-token",
                },
                "https://api.openai.com/profile": {
                    "email": "token-owner@example.com",
                },
            },
            "secret",
            algorithm="HS256",
        )
        team = Team(
            id=7,
            email="team-owner@example.com",
            access_token_encrypted="encrypted",
            account_id="acct-from-team",
        )

        payload = service._build_account_payload(
            team=team,
            access_token=access_token,
            refresh_token="rt-test",
            client_id="app_test_client",
        )

        self.assertEqual(payload["name"], "team-owner@example.com")
        self.assertEqual(payload["platform"], "openai")
        self.assertEqual(payload["type"], "oauth")
        self.assertEqual(payload["credentials"]["access_token"], access_token)
        self.assertEqual(payload["credentials"]["chatgpt_account_id"], "acct-from-team")
        self.assertEqual(payload["credentials"]["chatgpt_user_id"], "user-from-token")
        self.assertEqual(payload["credentials"]["client_id"], "app_test_client")
        self.assertEqual(payload["credentials"]["refresh_token"], "rt-test")
        self.assertEqual(payload["credentials"]["expires_at"], 1893456000)
        self.assertTrue(payload["extra"]["openai_passthrough"])
        self.assertEqual(payload["extra"]["email"], "team-owner@example.com")

    def test_build_account_payload_falls_back_to_token_email(self):
        service = Sub2apiService()
        access_token = jwt.encode(
            {
                "exp": 1893456000,
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "acct-from-token",
                },
                "https://api.openai.com/profile": {
                    "email": "token-owner@example.com",
                },
            },
            "secret",
            algorithm="HS256",
        )
        team = Team(
            id=8,
            email="",
            access_token_encrypted="encrypted",
            account_id="acct-from-team",
        )

        payload = service._build_account_payload(
            team=team,
            access_token=access_token,
            refresh_token="",
            client_id="",
        )

        self.assertEqual(payload["name"], "token-owner@example.com")
        self.assertEqual(payload["extra"]["email"], "token-owner@example.com")

    def test_is_same_payload_checks_runtime_fields(self):
        payload = {
            "name": "user@example.com",
            "credentials": {"access_token": "at-1"},
            "extra": {"email": "user@example.com"},
            "concurrency": 10,
            "priority": 1,
            "rate_multiplier": 1,
            "expires_at": 1893456000,
            "auto_pause_on_expired": True,
        }
        remote_account = {
            "name": "user@example.com",
            "credentials": {"access_token": "at-1"},
            "extra": {"email": "user@example.com"},
            "concurrency": 5,
            "priority": 1,
            "rate_multiplier": 1,
            "expires_at": 1893456000,
            "auto_pause_on_expired": True,
        }

        self.assertFalse(Sub2apiService._is_same_payload(remote_account, payload))
