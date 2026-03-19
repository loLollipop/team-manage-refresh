import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx
import pytz
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Team
from app.services.encryption import encryption_service
from app.services.settings import settings_service
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)


@dataclass
class CliproxyapiConfig:
    base_url: str
    api_key: str
    proxy: Optional[str] = None


class CliproxyapiService:
    MANAGEMENT_PREFIX = "/v0/management"
    DEFAULT_TIMEOUT = 20.0

    @staticmethod
    def normalize_base_url(base_url: Optional[str]) -> str:
        value = str(base_url or "").strip()
        if not value:
            return ""
        return value.rstrip("/")

    @staticmethod
    def is_valid_base_url(base_url: Optional[str]) -> bool:
        value = CliproxyapiService.normalize_base_url(base_url)
        if not value:
            return True

        try:
            parsed = urlparse(value)
        except Exception:
            return False

        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    @staticmethod
    def _sanitize_email_for_filename(email: str) -> str:
        normalized = str(email or "").strip().lower()
        sanitized = re.sub(r"[^A-Za-z0-9._@-]+", "_", normalized)
        return sanitized.strip("._-") or "team"

    @staticmethod
    def _canonical_json(payload: Dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _to_local_iso(dt) -> str:
        if not dt:
            return ""

        local_tz = pytz.timezone(settings.timezone)
        if dt.tzinfo is None:
            localized = local_tz.localize(dt)
        else:
            localized = dt.astimezone(local_tz)
        return localized.isoformat()

    async def _load_config(self, db_session: AsyncSession) -> Optional[CliproxyapiConfig]:
        base_url = self.normalize_base_url(
            await settings_service.get_setting(db_session, "cliproxyapi_base_url", "")
        )
        api_key = str(
            await settings_service.get_setting(db_session, "cliproxyapi_api_key", "")
            or ""
        ).strip()

        if not base_url or not api_key:
            return None

        proxy_config = await settings_service.get_proxy_config(db_session)
        proxy_url = proxy_config["proxy"] if proxy_config.get("enabled") and proxy_config.get("proxy") else None

        return CliproxyapiConfig(
            base_url=base_url,
            api_key=api_key,
            proxy=proxy_url,
        )

    @staticmethod
    def _build_warning_message(missing_fields: list[str]) -> str:
        if not missing_fields:
            return ""

        field_labels = {
            "id_token": "id_token",
            "refresh_token": "refresh_token",
        }
        labels = [field_labels.get(field, field) for field in missing_fields]
        joined = "、".join(labels)
        return f"当前 Team 缺少 {joined}，已按空值推送，CliproxyAPI 刷新额度时可能失败"

    def _build_payload(
        self,
        team: Team,
        access_token: str,
        id_token: str,
        refresh_token: str,
    ) -> Dict[str, Any]:
        # last_refresh 取同步时间而不是推送时间，避免重复推送因时间戳变化而失去幂等。
        last_refresh_time = team.last_sync or team.created_at or get_now()
        return {
            "access_token": access_token,
            "account_id": team.account_id or "",
            "email": team.email or "",
            "expired": self._to_local_iso(team.expires_at),
            "id_token": id_token,
            "last_refresh": self._to_local_iso(last_refresh_time),
            "refresh_token": refresh_token,
            "type": "codex",
        }

    def _build_filename(self, team: Team) -> str:
        safe_email = self._sanitize_email_for_filename(team.email or "")
        if team.expires_at:
            return f"{safe_email}__exp-{team.expires_at.strftime('%Y%m%d%H%M%S')}.json"
        return f"{safe_email}__team-{team.id}.json"

    def _normalize_downloaded_payload(self, content: str) -> Optional[Dict[str, Any]]:
        try:
            parsed = json.loads(content)
        except Exception:
            return None

        if isinstance(parsed, dict):
            return parsed
        return None

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        expected_status: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        response = await client.request(method, url, **kwargs)
        if expected_status is not None and response.status_code != expected_status:
            raise httpx.HTTPStatusError(
                f"unexpected status: {response.status_code}",
                request=response.request,
                response=response,
            )
        response.raise_for_status()
        if not response.content:
            return {}
        data = response.json()
        if isinstance(data, dict):
            return data
        raise ValueError("响应不是 JSON 对象")

    async def _get_remote_file(self, client: httpx.AsyncClient, base_url: str, filename: str) -> Optional[str]:
        url = f"{base_url}/auth-files/download"
        response = await client.get(url, params={"name": filename})
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.text

    async def _list_remote_files(self, client: httpx.AsyncClient, base_url: str) -> Dict[str, Any]:
        return await self._request_json(client, "GET", f"{base_url}/auth-files")

    async def _delete_remote_file(self, client: httpx.AsyncClient, base_url: str, filename: str) -> None:
        response = await client.delete(f"{base_url}/auth-files", params={"name": filename})
        if response.status_code == 404:
            return
        response.raise_for_status()

    async def _upload_remote_file(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        filename: str,
        canonical_payload: str,
    ) -> None:
        response = await client.post(
            f"{base_url}/auth-files",
            params={"name": filename},
            content=canonical_payload.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()

    async def push_team_auth_file(self, team_id: int, db_session: AsyncSession) -> Dict[str, Any]:
        config = await self._load_config(db_session)
        if not config:
            return {"success": False, "error": "请先在系统设置中填写 CliproxyAPI 地址和管理密钥"}

        if not self.is_valid_base_url(config.base_url):
            return {"success": False, "error": "CliproxyAPI 地址格式错误，仅支持 http/https"}

        result = await db_session.execute(select(Team).where(Team.id == team_id))
        team = result.scalar_one_or_none()
        if not team:
            return {"success": False, "error": "Team 不存在"}

        email = str(team.email or "").strip()
        if not email:
            return {"success": False, "error": "Team 缺少邮箱，无法生成认证文件", "email": ""}

        try:
            access_token = encryption_service.decrypt_token(team.access_token_encrypted)
        except Exception as exc:
            logger.error("解密 Team %s access_token 失败: %s", team_id, exc)
            access_token = ""

        if not access_token:
            return {"success": False, "error": "Team 缺少 Access Token，无法推送", "email": email}

        refresh_token = ""
        try:
            if team.refresh_token_encrypted:
                refresh_token = encryption_service.decrypt_token(team.refresh_token_encrypted)
        except Exception as exc:
            logger.warning("解密 Team %s refresh_token 失败，将按空值推送: %s", team_id, exc)

        id_token = ""
        try:
            if team.id_token_encrypted:
                id_token = encryption_service.decrypt_token(team.id_token_encrypted)
        except Exception as exc:
            logger.warning("解密 Team %s id_token 失败，将按空值推送: %s", team_id, exc)

        missing_fields = []
        if not id_token:
            missing_fields.append("id_token")
        if not refresh_token:
            missing_fields.append("refresh_token")
        warning_message = self._build_warning_message(missing_fields)

        filename = self._build_filename(team)
        payload = self._build_payload(team, access_token, id_token, refresh_token)
        canonical_payload = self._canonical_json(payload)
        management_base_url = f"{config.base_url}{self.MANAGEMENT_PREFIX}"

        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Accept": "application/json",
        }

        try:
            async with httpx.AsyncClient(
                timeout=self.DEFAULT_TIMEOUT,
                headers=headers,
                proxy=config.proxy,
            ) as client:
                listing = await self._list_remote_files(client, management_base_url)
                remote_files = listing.get("files") or []
                remote_entry = next(
                    (
                        item for item in remote_files
                        if isinstance(item, dict) and str(item.get("name") or "").strip() == filename
                    ),
                    None,
                )

                if remote_entry is None:
                    await self._upload_remote_file(client, management_base_url, filename, canonical_payload)
                    return {
                        "success": True,
                        "message": f"已推送到 CliproxyAPI：{filename}",
                        "email": email,
                        "filename": filename,
                        "action": "uploaded",
                        "warning": warning_message or None,
                        "warnings": missing_fields,
                    }

                if remote_entry.get("runtime_only"):
                    return {
                        "success": False,
                        "error": f"远端已存在同名 runtime_only 凭据，无法通过文件接口覆盖：{filename}",
                        "email": email,
                        "filename": filename,
                    }

                remote_content = await self._get_remote_file(client, management_base_url, filename)
                remote_payload = self._normalize_downloaded_payload(remote_content or "")

                if remote_payload is not None and remote_payload == payload:
                    return {
                        "success": True,
                        "message": f"远端认证文件已是最新，跳过推送：{filename}",
                        "email": email,
                        "filename": filename,
                        "action": "skipped",
                        "warning": warning_message or None,
                        "warnings": missing_fields,
                    }

                await self._delete_remote_file(client, management_base_url, filename)
                await self._upload_remote_file(client, management_base_url, filename, canonical_payload)
                return {
                    "success": True,
                    "message": f"已更新远端认证文件：{filename}",
                    "email": email,
                    "filename": filename,
                    "action": "updated",
                    "warning": warning_message or None,
                    "warnings": missing_fields,
                }
        except httpx.HTTPStatusError as exc:
            response_text = ""
            try:
                response_text = exc.response.text.strip()
            except Exception:
                response_text = ""

            logger.error(
                "推送 Team %s 到 CliproxyAPI 失败，status=%s, body=%s",
                team_id,
                getattr(exc.response, "status_code", "unknown"),
                response_text,
            )
            error_message = response_text or f"HTTP {getattr(exc.response, 'status_code', 'unknown')}"
            return {"success": False, "error": f"CliproxyAPI 请求失败: {error_message}", "email": email, "filename": filename}
        except Exception as exc:
            logger.error("推送 Team %s 到 CliproxyAPI 异常: %s", team_id, exc)
            return {"success": False, "error": f"推送失败: {str(exc)}", "email": email, "filename": filename}


cliproxyapi_service = CliproxyapiService()
