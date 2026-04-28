import json
import unittest
from unittest.mock import patch

from fastapi.responses import StreamingResponse

from app.routes import admin


class _FakeSessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _fake_async_session_local():
    return _FakeSessionContext()


class AdminBatchRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_refresh_uses_non_force_sync_like_single_refresh(self):
        calls = []

        async def stub_sync(team_id, db_session, force_refresh=False):
            calls.append((team_id, force_refresh))
            return {
                "success": True,
                "message": f"同步成功 {team_id}",
                "error": None,
            }

        with patch("app.routes.admin.AsyncSessionLocal", new=_fake_async_session_local), \
             patch.object(admin.team_service, "sync_team_info", new=stub_sync):
            response = await admin.batch_refresh_teams(
                admin.BatchRefreshRequest(ids=[11, 22]),
                current_user={"username": "admin", "is_admin": True},
            )

            self.assertIsInstance(response, StreamingResponse)

            chunks = []
            async for chunk in response.body_iterator:
                if isinstance(chunk, (bytes, bytearray)):
                    chunks.append(chunk.decode("utf-8"))
                else:
                    chunks.append(str(chunk))

        payloads = [json.loads(line) for line in "".join(chunks).splitlines() if line.strip()]
        self.assertEqual(payloads[-1]["type"], "finish")
        self.assertEqual({team_id for team_id, _ in calls}, {11, 22})
        self.assertTrue(all(force_refresh is False for _, force_refresh in calls))
