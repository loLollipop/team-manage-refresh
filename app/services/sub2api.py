import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Team
from app.services.encryption import encryption_service
from app.services.settings import settings_service
from app.utils.jwt_parser import JWTParser
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)


@dataclass
class Sub2apiConfig:
    base_url: str
    admin_api_key: str
    proxy: Optional[str] = None


class Sub2apiService:
    API_PREFIX = "/api/v1"
    DEFAULT_TIMEOUT = 20.0

    def __init__(self):
        self.jwt_parser = JWTParser()

    @staticmethod
    def normalize_base_url(base_url: Optional[str]) -> str:
        value = str(base_url or "").strip().rstrip("/")
        if value.endswith("/api/v1"):
            value = value[:-7]
        return value

    @staticmethod
    def normalize_admin_api_key(value: Optional[str]) -> str:
        value = str(value or "").strip()
        if value.lower().startswith("bearer "):
            value = value[7:].strip()
        return value

    @staticmethod
    def is_valid_base_url(base_url: Optional[str]) -> bool:
        value = Sub2apiService.normalize_base_url(base_url)
        if not value:
            return True

        try:
            parsed = urlparse(value)
        except Exception:
            return False

        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    async def _load_config(self, db_session: AsyncSession) -> Optional[Sub2apiConfig]:
        base_url = self.normalize_base_url(
            await settings_service.get_setting(db_session, "sub2api_base_url", "")
        )
        admin_api_key = self.normalize_admin_api_key(
            await settings_service.get_setting(db_session, "sub2api_access_token", "")
        )

        if not base_url or not admin_api_key:
            return None

        proxy_config = await settings_service.get_proxy_config(db_session)
        proxy_url = proxy_config["proxy"] if proxy_config.get("enabled") and proxy_config.get("proxy") else None

        return Sub2apiConfig(
            base_url=base_url,
            admin_api_key=admin_api_key,
            proxy=proxy_url,
        )

    @staticmethod
    def _build_request_headers(admin_api_key: str) -> Dict[str, str]:
        return {
            "x-api-key": admin_api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _unwrap_response_data(payload: Any) -> Any:
        if isinstance(payload, dict) and "code" in payload:
            return payload.get("data")
        return payload

    @staticmethod
    def _build_warning_message(missing_fields: list[str]) -> str:
        if not missing_fields:
            return ""

        field_labels = {
            "refresh_token": "refresh_token",
            "client_id": "client_id",
            "account_id": "account_id",
        }
        joined = "、".join(field_labels.get(field, field) for field in missing_fields)
        return f"当前 Team 缺少 {joined}，sub2api 中 OAuth 刷新或账号识别可能受影响"

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        response = await client.request(method, url, **kwargs)
        response.raise_for_status()
        if not response.content:
            return {}
        data = response.json()
        if isinstance(data, dict):
            return data
        raise ValueError("响应不是 JSON 对象")

    def _build_account_payload(
        self,
        team: Team,
        access_token: str,
        refresh_token: str,
        client_id: str,
    ) -> Dict[str, Any]:
        decoded = self.jwt_parser.decode_token(access_token) or {}
        auth_claims = decoded.get("https://api.openai.com/auth", {}) or {}
        profile_claims = decoded.get("https://api.openai.com/profile", {}) or {}

        account_id = (
            str(team.account_id or "").strip()
            or str(auth_claims.get("chatgpt_account_id") or "").strip()
        )
        user_id = (
            str(auth_claims.get("chatgpt_user_id") or "").strip()
            or str(auth_claims.get("user_id") or "").strip()
        )
        expires_at = decoded.get("exp")
        if not isinstance(expires_at, int):
            expires_at = None

        expires_in = None
        if expires_at is not None:
            expires_in = max(expires_at - int(get_now().timestamp()), 0)

        credentials: Dict[str, Any] = {
            "access_token": access_token,
        }
        if account_id:
            credentials["chatgpt_account_id"] = account_id
        if user_id:
            credentials["chatgpt_user_id"] = user_id
        if client_id:
            credentials["client_id"] = client_id
        if expires_at is not None:
            credentials["expires_at"] = expires_at
        if expires_in is not None:
            credentials["expires_in"] = expires_in
        if refresh_token:
            credentials["refresh_token"] = refresh_token

        extra: Dict[str, Any] = {
            "openai_passthrough": True,
        }
        email = str(team.email or "").strip() or str(profile_claims.get("email") or "").strip()
        if email:
            extra["email"] = email

        return {
            "name": email or account_id or f"team-{team.id}",
            "platform": "openai",
            "type": "oauth",
            "credentials": credentials,
            "extra": extra,
            "concurrency": 10,
            "priority": 1,
            "rate_multiplier": 1,
            "expires_at": expires_at,
            "auto_pause_on_expired": True,
        }

    @staticmethod
    def _extract_payload_email(payload: Dict[str, Any]) -> str:
        extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
        return str(extra.get("email") or "").strip()

    @staticmethod
    def _match_existing_account(item: Dict[str, Any], email: str, account_id: str) -> bool:
        if not isinstance(item, dict):
            return False

        normalized_email = str(email or "").strip().lower()
        normalized_account_id = str(account_id or "").strip()

        name = str(item.get("name") or "").strip().lower()
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        credentials = item.get("credentials") if isinstance(item.get("credentials"), dict) else {}
        extra_email = str(extra.get("email") or "").strip().lower()
        remote_account_id = str(
            credentials.get("chatgpt_account_id")
            or credentials.get("account_id")
            or ""
        ).strip()

        if normalized_account_id and remote_account_id == normalized_account_id:
            return True
        if normalized_email and (name == normalized_email or extra_email == normalized_email):
            return True
        return False

    async def _find_existing_account(
        self,
        client: httpx.AsyncClient,
        api_base_url: str,
        *,
        email: str,
        account_id: str,
    ) -> Optional[Dict[str, Any]]:
        searched_keywords = []
        for keyword in (email, account_id):
            normalized = str(keyword or "").strip()
            if normalized and normalized not in searched_keywords:
                searched_keywords.append(normalized)

        for keyword in searched_keywords:
            payload = await self._request_json(
                client,
                "GET",
                f"{api_base_url}/admin/accounts",
                params={
                    "page": 1,
                    "page_size": 100,
                    "platform": "openai",
                    "type": "oauth",
                    "search": keyword,
                },
            )
            data = self._unwrap_response_data(payload)
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                continue

            for item in items:
                if self._match_existing_account(item, email, account_id):
                    return item

        return None

    @staticmethod
    def _is_same_payload(
        remote_account: Dict[str, Any],
        payload: Dict[str, Any],
    ) -> bool:
        if not isinstance(remote_account, dict):
            return False

        remote_credentials = remote_account.get("credentials") if isinstance(remote_account.get("credentials"), dict) else {}
        remote_extra = remote_account.get("extra") if isinstance(remote_account.get("extra"), dict) else {}
        target_credentials = payload.get("credentials") if isinstance(payload.get("credentials"), dict) else {}
        target_extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}

        for key, value in target_credentials.items():
            if remote_credentials.get(key) != value:
                return False

        for key, value in target_extra.items():
            if remote_extra.get(key) != value:
                return False

        if str(remote_account.get("name") or "").strip() != str(payload.get("name") or "").strip():
            return False

        remote_expires_at = remote_account.get("expires_at")
        target_expires_at = payload.get("expires_at")
        if target_expires_at is not None and remote_expires_at != target_expires_at:
            return False

        comparable_fields = (
            "concurrency",
            "priority",
            "rate_multiplier",
            "auto_pause_on_expired",
        )
        for field in comparable_fields:
            if remote_account.get(field) != payload.get(field):
                return False

        return True

    async def push_team_account(self, team_id: int, db_session: AsyncSession) -> Dict[str, Any]:
        config = await self._load_config(db_session)
        if not config:
            return {"success": False, "error": "请先在系统设置中填写 sub2api 地址和管理员 API Key"}

        if not self.is_valid_base_url(config.base_url):
            return {"success": False, "error": "sub2api 地址格式错误，仅支持 http/https"}

        result = await db_session.execute(select(Team).where(Team.id == team_id))
        team = result.scalar_one_or_none()
        if not team:
            return {"success": False, "error": "Team 不存在"}

        try:
            access_token = encryption_service.decrypt_token(team.access_token_encrypted)
        except Exception as exc:
            logger.error("解密 Team %s access_token 失败: %s", team_id, exc)
            access_token = ""

        if not access_token:
            return {"success": False, "error": "Team 缺少 Access Token，无法推送", "email": str(team.email or "").strip()}

        refresh_token = ""
        try:
            if team.refresh_token_encrypted:
                refresh_token = encryption_service.decrypt_token(team.refresh_token_encrypted)
        except Exception as exc:
            logger.warning("解密 Team %s refresh_token 失败，将按空值推送: %s", team_id, exc)

        client_id = str(team.client_id or "").strip()

        payload = self._build_account_payload(team, access_token, refresh_token, client_id)
        email = self._extract_payload_email(payload)
        credentials = payload.get("credentials", {})
        missing_fields = []
        if not refresh_token:
            missing_fields.append("refresh_token")
        if not client_id:
            missing_fields.append("client_id")
        if not credentials.get("chatgpt_account_id"):
            missing_fields.append("account_id")
        warning_message = self._build_warning_message(missing_fields)

        headers = self._build_request_headers(config.admin_api_key)
        api_base_url = f"{config.base_url}{self.API_PREFIX}"

        try:
            async with httpx.AsyncClient(
                timeout=self.DEFAULT_TIMEOUT,
                headers=headers,
                proxy=config.proxy,
            ) as client:
                existing_account = await self._find_existing_account(
                    client,
                    api_base_url,
                    email=email,
                    account_id=str(credentials.get("chatgpt_account_id") or ""),
                )

                if existing_account and self._is_same_payload(existing_account, payload):
                    return {
                        "success": True,
                        "message": f"sub2api 账号已是最新，跳过推送：{payload['name']}",
                        "email": email,
                        "account_name": payload["name"],
                        "remote_id": existing_account.get("id"),
                        "action": "skipped",
                        "warning": warning_message or None,
                        "warnings": missing_fields,
                    }

                if existing_account:
                    remote_credentials = existing_account.get("credentials") if isinstance(existing_account.get("credentials"), dict) else {}
                    remote_extra = existing_account.get("extra") if isinstance(existing_account.get("extra"), dict) else {}
                    update_payload = {
                        "name": payload["name"],
                        "credentials": {**remote_credentials, **payload["credentials"]},
                        "extra": {**remote_extra, **payload["extra"]},
                        "concurrency": payload.get("concurrency"),
                        "priority": payload.get("priority"),
                        "rate_multiplier": payload.get("rate_multiplier"),
                        "expires_at": payload.get("expires_at"),
                        "auto_pause_on_expired": payload.get("auto_pause_on_expired"),
                    }
                    response_payload = await self._request_json(
                        client,
                        "PUT",
                        f"{api_base_url}/admin/accounts/{existing_account['id']}",
                        json=update_payload,
                    )
                    updated = self._unwrap_response_data(response_payload) or {}
                    return {
                        "success": True,
                        "message": f"已更新 sub2api 账号：{payload['name']}",
                        "email": email,
                        "account_name": payload["name"],
                        "remote_id": updated.get("id") or existing_account.get("id"),
                        "action": "updated",
                        "warning": warning_message or None,
                        "warnings": missing_fields,
                    }

                response_payload = await self._request_json(
                    client,
                    "POST",
                    f"{api_base_url}/admin/accounts",
                    json=payload,
                )
                created = self._unwrap_response_data(response_payload) or {}
                return {
                    "success": True,
                    "message": f"已推送到 sub2api：{payload['name']}",
                    "email": email,
                    "account_name": payload["name"],
                    "remote_id": created.get("id"),
                    "action": "created",
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
                "推送 Team %s 到 sub2api 失败，status=%s, body=%s",
                team_id,
                getattr(exc.response, "status_code", "unknown"),
                response_text,
            )
            error_message = response_text or f"HTTP {getattr(exc.response, 'status_code', 'unknown')}"
            return {
                "success": False,
                "error": f"sub2api 请求失败: {error_message}",
                "email": email,
                "account_name": payload["name"],
            }
        except Exception as exc:
            logger.error("推送 Team %s 到 sub2api 异常: %s", team_id, exc)
            return {
                "success": False,
                "error": f"推送失败: {str(exc)}",
                "email": email,
                "account_name": payload["name"],
            }


sub2api_service = Sub2apiService()
