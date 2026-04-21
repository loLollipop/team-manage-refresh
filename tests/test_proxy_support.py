import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.services.chatgpt import ChatGPTService
from app.services.cliproxyapi import CliproxyapiService
from app.services.notification import NotificationService
from app.services.settings import settings_service
from app.routes.admin import ProxyConfigRequest, update_proxy_config
from app.utils.proxy import build_curl_cffi_proxies, build_httpx_proxy, mask_proxy_url, normalize_proxy_url


class FakeCurlSession:
    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        type(self).instances.append(self)

    async def close(self):
        return None


class FakeHTTPXResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text or (json.dumps(json_data) if json_data is not None else "")
        self.content = self.text.encode("utf-8") if self.text else b""
        self.request = httpx.Request("GET", "https://example.com")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"unexpected status: {self.status_code}",
                request=self.request,
                response=self,
            )

    def json(self):
        return self._json_data


class FakeHTTPXAsyncClient:
    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.requests = []
        self.posts = []
        type(self).instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return FakeHTTPXResponse(json_data={"files": []})

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeHTTPXResponse(status_code=200)


class DummySessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class AsyncDatabaseTestCase(unittest.IsolatedAsyncioTestCase):
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

    async def asyncTearDown(self):
        settings_service.clear_cache()
        await self.engine.dispose()

    async def set_proxy_config(self, enabled, proxy):
        async with self.session_factory() as session:
            await settings_service.update_proxy_config(session, enabled, proxy)

    async def set_settings(self, values):
        async with self.session_factory() as session:
            await settings_service.update_settings(session, values)


class ProxyHelperTests(unittest.TestCase):
    def test_normalize_proxy_url_strips_whitespace(self):
        self.assertEqual(
            normalize_proxy_url("  socks5h://127.0.0.1:1080  "),
            "socks5h://127.0.0.1:1080",
        )

    def test_normalize_proxy_url_rejects_invalid_scheme(self):
        with self.assertRaises(ValueError):
            normalize_proxy_url("ftp://127.0.0.1:21")

    def test_normalize_proxy_url_requires_hostname(self):
        with self.assertRaises(ValueError):
            normalize_proxy_url("socks5h://:1080")

    def test_normalize_proxy_url_rejects_invalid_port(self):
        with self.assertRaises(ValueError):
            normalize_proxy_url("socks5://127.0.0.1:bad")

    def test_build_proxy_helpers_return_none_for_empty_value(self):
        self.assertIsNone(build_httpx_proxy("   "))
        self.assertIsNone(build_curl_cffi_proxies(None))

    def test_mask_proxy_url_hides_credentials(self):
        self.assertEqual(
            mask_proxy_url("socks5://user:secret@127.0.0.1:1080"),
            "socks5://***:***@127.0.0.1:1080",
        )


class AdminProxyValidationTests(AsyncDatabaseTestCase):
    async def test_update_proxy_config_rejects_empty_proxy_when_enabled(self):
        async with self.session_factory() as session:
            response = await update_proxy_config(
                ProxyConfigRequest(enabled=True, proxy="   "),
                db=session,
                current_user={"username": "admin"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.body.decode("utf-8"), '{"success":false,"error":"启用代理时必须填写代理地址"}')

    async def test_update_proxy_config_rejects_proxy_without_hostname(self):
        async with self.session_factory() as session:
            response = await update_proxy_config(
                ProxyConfigRequest(enabled=True, proxy="socks5h://:1080"),
                db=session,
                current_user={"username": "admin"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("代理地址格式错误", response.body.decode("utf-8"))

    async def test_update_proxy_config_rejects_proxy_with_invalid_port(self):
        async with self.session_factory() as session:
            response = await update_proxy_config(
                ProxyConfigRequest(enabled=True, proxy="socks5://127.0.0.1:bad"),
                db=session,
                current_user={"username": "admin"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("代理地址格式错误", response.body.decode("utf-8"))


class ChatGPTProxySupportTests(AsyncDatabaseTestCase):
    async def test_create_session_preserves_socks5h_proxy(self):
        await self.set_proxy_config(True, "socks5h://127.0.0.1:1080")
        FakeCurlSession.instances.clear()
        service = ChatGPTService()

        async with self.session_factory() as session:
            with patch("app.services.chatgpt.AsyncSession", new=FakeCurlSession):
                await service._create_session(session)

        proxies = FakeCurlSession.instances[0].kwargs["proxies"]
        self.assertEqual(proxies["all"], "socks5h://127.0.0.1:1080")
        self.assertEqual(proxies["http"], "socks5h://127.0.0.1:1080")
        self.assertEqual(proxies["https"], "socks5h://127.0.0.1:1080")

    async def test_create_session_omits_proxy_when_disabled(self):
        await self.set_proxy_config(False, "socks5://127.0.0.1:1080")
        FakeCurlSession.instances.clear()
        service = ChatGPTService()

        async with self.session_factory() as session:
            with patch("app.services.chatgpt.AsyncSession", new=FakeCurlSession):
                await service._create_session(session)

        self.assertIsNone(FakeCurlSession.instances[0].kwargs["proxies"])


class CliproxyapiProxySupportTests(AsyncDatabaseTestCase):
    async def test_push_team_auth_file_uses_socks5h_proxy(self):
        await self.set_settings(
            {
                "cliproxyapi_base_url": "https://cliproxy.example.com",
                "cliproxyapi_api_key": "secret-key",
                "proxy_enabled": "true",
                "proxy": "socks5h://127.0.0.1:1080",
            }
        )
        FakeHTTPXAsyncClient.instances.clear()
        service = CliproxyapiService()

        payload = {
            "success": True,
            "team_id": 1,
            "email": "owner@example.com",
            "filename": "owner.json",
            "payload": {"access_token": "token"},
            "warning": None,
            "warnings": [],
        }

        async with self.session_factory() as session:
            with patch.object(service, "get_team_auth_file_data", AsyncMock(return_value=payload)):
                with patch("app.services.cliproxyapi.httpx.AsyncClient", new=FakeHTTPXAsyncClient):
                    result = await service.push_team_auth_file(1, session)

        self.assertTrue(result["success"])
        self.assertEqual(
            FakeHTTPXAsyncClient.instances[0].kwargs["proxy"],
            "socks5h://127.0.0.1:1080",
        )

    async def test_push_team_auth_file_omits_proxy_when_disabled(self):
        await self.set_settings(
            {
                "cliproxyapi_base_url": "https://cliproxy.example.com",
                "cliproxyapi_api_key": "secret-key",
                "proxy_enabled": "false",
                "proxy": "socks5://127.0.0.1:1080",
            }
        )
        FakeHTTPXAsyncClient.instances.clear()
        service = CliproxyapiService()

        payload = {
            "success": True,
            "team_id": 1,
            "email": "owner@example.com",
            "filename": "owner.json",
            "payload": {"access_token": "token"},
            "warning": None,
            "warnings": [],
        }

        async with self.session_factory() as session:
            with patch.object(service, "get_team_auth_file_data", AsyncMock(return_value=payload)):
                with patch("app.services.cliproxyapi.httpx.AsyncClient", new=FakeHTTPXAsyncClient):
                    result = await service.push_team_auth_file(1, session)

        self.assertTrue(result["success"])
        self.assertIsNone(FakeHTTPXAsyncClient.instances[0].kwargs["proxy"])


class NotificationProxySupportTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_and_notify_low_stock_uses_socks5h_proxy(self):
        FakeHTTPXAsyncClient.instances.clear()
        service = NotificationService()

        async def fake_get_setting(_session, key, default=None, use_cache=True):
            values = {
                "webhook_url": "https://example.com/webhook",
                "low_stock_threshold": "10",
                "api_key": "api-key",
            }
            return values.get(key, default)

        with patch("app.services.notification.AsyncSessionLocal", return_value=DummySessionContext()):
            with patch("app.services.notification.settings_service.get_setting", new=fake_get_setting):
                with patch(
                    "app.services.notification.settings_service.get_proxy_config",
                    new=AsyncMock(return_value={"enabled": True, "proxy": "socks5h://127.0.0.1:1080"}),
                ):
                    with patch(
                        "app.services.notification.team_service.get_total_available_seats",
                        new=AsyncMock(return_value=0),
                    ):
                        with patch(
                            "app.services.notification.httpx.AsyncClient",
                            new=FakeHTTPXAsyncClient,
                        ):
                            success = await service.check_and_notify_low_stock()

        self.assertTrue(success)
        self.assertEqual(
            FakeHTTPXAsyncClient.instances[0].kwargs["proxy"],
            "socks5h://127.0.0.1:1080",
        )

    async def test_check_and_notify_low_stock_omits_proxy_when_disabled(self):
        FakeHTTPXAsyncClient.instances.clear()
        service = NotificationService()

        async def fake_get_setting(_session, key, default=None, use_cache=True):
            values = {
                "webhook_url": "https://example.com/webhook",
                "low_stock_threshold": "10",
                "api_key": "api-key",
            }
            return values.get(key, default)

        with patch("app.services.notification.AsyncSessionLocal", return_value=DummySessionContext()):
            with patch("app.services.notification.settings_service.get_setting", new=fake_get_setting):
                with patch(
                    "app.services.notification.settings_service.get_proxy_config",
                    new=AsyncMock(return_value={"enabled": False, "proxy": "socks5://127.0.0.1:1080"}),
                ):
                    with patch(
                        "app.services.notification.team_service.get_total_available_seats",
                        new=AsyncMock(return_value=0),
                    ):
                        with patch(
                            "app.services.notification.httpx.AsyncClient",
                            new=FakeHTTPXAsyncClient,
                        ):
                            success = await service.check_and_notify_low_stock()

        self.assertTrue(success)
        self.assertIsNone(FakeHTTPXAsyncClient.instances[0].kwargs["proxy"])


if __name__ == "__main__":
    unittest.main()
