"""
ChatGPT API 服务
用于调用 ChatGPT 后端 API,实现 Team 成员管理功能
"""
import asyncio
import base64
import hashlib
import logging
import random
import secrets
from urllib.parse import urlencode, urlparse
from typing import Optional, Dict, Any, List
from curl_cffi.requests import AsyncSession
from app.services.settings import settings_service
from sqlalchemy.ext.asyncio import AsyncSession as DBAsyncSession
from app.utils.jwt_parser import JWTParser
from app.utils.proxy import build_curl_cffi_proxies

logger = logging.getLogger(__name__)


class ChatGPTService:
    """ChatGPT API 服务类"""

    BASE_URL = "https://chatgpt.com/backend-api"

    # 重试配置
    MAX_RETRIES = 3
    RETRY_DELAYS = [1, 2, 4]  # 指数退避: 1s, 2s, 4s

    def __init__(self):
        """初始化 ChatGPT API 服务"""
        self.jwt_parser = JWTParser()
        # 会话池：按标识符（如 Email 或 TeamID）隔离，防止身份泄漏并提高 CF 稳定性
        self._sessions: Dict[str, AsyncSession] = {}
        self.proxy: Optional[str] = None

    async def _get_proxy_config(self, db_session: DBAsyncSession) -> Optional[str]:
        """
        获取代理配置
        """
        proxy_config = await settings_service.get_proxy_config(db_session)
        if proxy_config["enabled"] and proxy_config["proxy"]:
            return proxy_config["proxy"]
        return None

    async def _create_session(self, db_session: DBAsyncSession) -> AsyncSession:
        """
        创建 HTTP 会话
        """
        proxy = await self._get_proxy_config(db_session)

        proxies = build_curl_cffi_proxies(proxy)
        if proxies:
            normalized_proxy = proxies["all"]

            try:
                parsed_proxy = urlparse(normalized_proxy)
                proxy_scheme = parsed_proxy.scheme or "unknown"
                proxy_host = parsed_proxy.hostname or ""
                proxy_port = parsed_proxy.port
                logger.info(
                    "创建 ChatGPT 会话代理: scheme=%s host=%s port=%s via=all/http/https",
                    proxy_scheme,
                    proxy_host,
                    proxy_port,
                )
            except Exception:
                logger.info("创建 ChatGPT 会话代理: %s", normalized_proxy)
        else:
            logger.info("创建 ChatGPT 会话代理: disabled")

        # 使用 chrome110 指纹，这是 curl_cffi 中绕过 CF 最稳定的版本之一
        session = AsyncSession(
            impersonate="chrome110",
            proxies=proxies,
            timeout=30,
            verify=False # 某些代理环境下需要，或根据需求开启
        )
        return session

    async def _get_session(self, db_session: DBAsyncSession, identifier: str) -> AsyncSession:
        """
        根据标识符获取或创建持久会话
        """
        if identifier not in self._sessions:
            logger.info(f"为标识符 {identifier} 创建新会话")
            self._sessions[identifier] = await self._create_session(db_session)
        return self._sessions[identifier]

    async def _make_request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        json_data: Optional[Dict[str, Any]] = None,
        db_session: Optional[DBAsyncSession] = None,
        identifier: str = "default"
    ) -> Dict[str, Any]:
        """
        发送 HTTP 请求 (使用持久化隔离会话，提高 CF 通过率并防止污染)
        """
        # 尝试从 Header 或 Token 自动提取标识符，确保身份绝对隔离
        if identifier == "default":
            # 优先从账号 ID 识别，这对 Team 邀请等操作最重要
            acc_id = headers.get("chatgpt-account-id")
            if acc_id:
                identifier = f"acc_{acc_id}"
            # 其次从 Token 解析邮箱
            elif "Authorization" in headers:
                token = headers["Authorization"].replace("Bearer ", "")
                email = self.jwt_parser.extract_email(token)
                if email:
                    identifier = email

        session = await self._get_session(db_session, identifier)
        
        # 补全基础浏览器请求头
        base_headers = {
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://chatgpt.com/",
            "Origin": "https://chatgpt.com",
            "Connection": "keep-alive"
        }
        # 合并请求头，不要轻易覆盖 User-Agent 以免破坏 impersonate 的指纹
        for k, v in base_headers.items():
            if k not in headers:
                headers[k] = v

        for attempt in range(self.MAX_RETRIES):
            try:
                # 随机微小延迟，模拟真实用户行为
                if attempt > 0:
                    delay = self.RETRY_DELAYS[attempt-1] + random.uniform(0.5, 1.5)
                    await asyncio.sleep(delay)

                logger.info(f"[{identifier}] 发送请求: {method} {url} (尝试 {attempt + 1})")

                if method == "GET":
                    response = await session.get(url, headers=headers)
                elif method == "POST":
                    response = await session.post(url, headers=headers, json=json_data)
                elif method == "DELETE":
                    response = await session.delete(url, headers=headers, json=json_data)
                else:
                    raise ValueError(f"不支持的 HTTP 方法: {method}")

                status_code = response.status_code
                logger.info(f"响应状态码: {status_code}")

                if 200 <= status_code < 300:
                    try:
                        data = response.json()
                    except Exception:
                        data = {}
                    return {"success": True, "status_code": status_code, "data": data, "error": None}

                if 400 <= status_code < 500:
                    error_msg = response.text
                    error_code = None
                    try:
                        error_data = response.json()
                        detail = error_data.get("detail", error_msg)
                        # 确保 error_msg 是字符串，避免前端显示 [object Object]
                        error_msg = str(detail) if not isinstance(detail, str) else detail
                        if isinstance(error_data, dict):
                            error_info = error_data.get("error")
                            error_code = error_info.get("code") if isinstance(error_info, dict) else error_data.get("code")
                    except Exception:
                        pass
                    
                    if error_code == "token_invalidated" or "token_invalidated" in str(error_msg).lower():
                        logger.warning(f"检测到 Token 失效，清理会话缓存: {identifier}")
                        await self.clear_session(identifier)
                    
                    logger.warning(f"客户端错误 {status_code}: {error_msg}")
                    return {"success": False, "status_code": status_code, "error": error_msg, "error_code": error_code}

                if status_code >= 500:
                    if attempt < self.MAX_RETRIES - 1:
                        continue
                    return {"success": False, "status_code": status_code, "error": f"服务器错误 {status_code}"}

            except Exception as e:
                logger.error(f"请求异常: {e}")
                if attempt < self.MAX_RETRIES - 1:
                    continue
                return {"success": False, "status_code": 0, "error": str(e)}

        return {"success": False, "status_code": 0, "error": "未知错误"}

    async def send_invite(
        self,
        access_token: str,
        account_id: str,
        email: str,
        db_session: DBAsyncSession,
        identifier: str = "default"
    ) -> Dict[str, Any]:
        """发送 Team 邀请"""
        url = f"{self.BASE_URL}/accounts/{account_id}/invites"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id
        }
        json_data = {"email_addresses": [email], "role": "standard-user", "resend_emails": True}
        return await self._make_request("POST", url, headers, json_data, db_session, identifier)

    async def get_members(
        self,
        access_token: str,
        account_id: str,
        db_session: DBAsyncSession,
        identifier: str = "default"
    ) -> Dict[str, Any]:
        """获取 Team 成员列表"""
        all_members = []
        offset = 0
        limit = 50
        while True:
            url = f"{self.BASE_URL}/accounts/{account_id}/users?limit={limit}&offset={offset}"
            headers = {"Authorization": f"Bearer {access_token}"}
            result = await self._make_request("GET", url, headers, db_session=db_session, identifier=identifier)
            if not result["success"]:
                return {
                    "success": False,
                    "members": [],
                    "total": 0,
                    "error": result["error"],
                    "error_code": result.get("error_code"),
                    "status_code": result.get("status_code"),
                }
            data = result["data"]
            items = data.get("items", [])
            total = data.get("total", 0)
            all_members.extend(items)
            if len(all_members) >= total:
                break
            offset += limit
        return {"success": True, "members": all_members, "total": len(all_members), "error": None}

    async def get_invites(
        self,
        access_token: str,
        account_id: str,
        db_session: DBAsyncSession,
        identifier: str = "default"
    ) -> Dict[str, Any]:
        """获取 Team 邀请列表"""
        url = f"{self.BASE_URL}/accounts/{account_id}/invites"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id
        }
        result = await self._make_request("GET", url, headers, db_session=db_session, identifier=identifier)
        if not result["success"]:
            return {
                "success": False,
                "items": [],
                "total": 0,
                "error": result["error"],
                "error_code": result.get("error_code"),
                "status_code": result.get("status_code"),
            }
        data = result["data"]
        items = data.get("items", [])
        return {"success": True, "items": items, "total": len(items), "error": None}

    async def delete_invite(
        self,
        access_token: str,
        account_id: str,
        email: str,
        db_session: DBAsyncSession,
        identifier: str = "default"
    ) -> Dict[str, Any]:
        """撤回邀请"""
        url = f"{self.BASE_URL}/accounts/{account_id}/invites"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id
        }
        json_data = {"email_address": email}
        return await self._make_request("DELETE", url, headers, json_data, db_session, identifier)

    async def delete_member(
        self,
        access_token: str,
        account_id: str,
        user_id: str,
        db_session: DBAsyncSession,
        identifier: str = "default"
    ) -> Dict[str, Any]:
        """删除成员"""
        url = f"{self.BASE_URL}/accounts/{account_id}/users/{user_id}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id
        }
        result = await self._make_request("DELETE", url, headers, db_session=db_session, identifier=identifier)
        return result

    async def toggle_beta_feature(
        self,
        access_token: str,
        account_id: str,
        feature: str,
        value: bool,
        db_session: DBAsyncSession,
        identifier: str = "default"
    ) -> Dict[str, Any]:
        """开启或关闭 Beta 功能"""
        url = f"{self.BASE_URL}/accounts/{account_id}/beta_features"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id,
            "oai-language": "zh-CN",
            "sec-ch-ua-platform": '"Windows"'
        }
        json_data = {"feature": feature, "value": value}
        return await self._make_request("POST", url, headers, json_data, db_session, identifier)

    async def get_account_info(
        self,
        access_token: str,
        db_session: DBAsyncSession,
        identifier: str = "default"
    ) -> Dict[str, Any]:
        """获取账户和订阅信息"""
        url = f"{self.BASE_URL}/accounts/check/v4-2023-04-27"
        headers = {"Authorization": f"Bearer {access_token}"}
        result = await self._make_request("GET", url, headers, db_session=db_session, identifier=identifier)
        if not result["success"]:
            return {
                "success": False,
                "accounts": [],
                "error": result["error"],
                "error_code": result.get("error_code"),
                "status_code": result.get("status_code"),
            }
        
        data = result["data"]
        accounts_data = data.get("accounts", {})
        team_accounts = []
        for aid, info in accounts_data.items():
            account = info.get("account", {})
            entitlement = info.get("entitlement", {})
            if account.get("plan_type") == "team":
                team_accounts.append({
                    "account_id": aid,
                    "name": account.get("name", ""),
                    "plan_type": "team",
                    "account_user_role": account.get("account_user_role", ""),
                    "subscription_plan": entitlement.get("subscription_plan", ""),
                    "expires_at": entitlement.get("expires_at", ""),
                    "has_active_subscription": entitlement.get("has_active_subscription", False)
                })
        return {"success": True, "accounts": team_accounts, "error": None}

    async def get_account_settings(
        self,
        access_token: str,
        account_id: str,
        db_session: DBAsyncSession,
        identifier: str = "default"
    ) -> Dict[str, Any]:
        """获取账户设置信息 (包含 beta_settings)"""
        url = f"{self.BASE_URL}/accounts/{account_id}/settings"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id
        }
        return await self._make_request("GET", url, headers, db_session=db_session, identifier=identifier)

    async def refresh_access_token_with_session_token(
        self,
        session_token: str,
        db_session: DBAsyncSession,
        account_id: Optional[str] = None,
        identifier: str = "default"
    ) -> Dict[str, Any]:
        """使用 session_token 刷新 AT (使用标识符隔离会话)"""
        url = "https://chatgpt.com/api/auth/session"
        if account_id:
            url += f"?exchange_workspace_token=true&workspace_id={account_id}&reason=setCurrentAccount"
            
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Cookie": f"__Secure-next-auth.session-token={session_token}"
        }
        
        # 对于刷新请求，如果未提供 identifier，我们使用 session_token 的前 8 位作为临时隔离
        if identifier == "default":
            identifier = f"st_{session_token[:8]}"

        session = await self._get_session(db_session, identifier)
        try:
            # 手动合并基础头
            headers["Referer"] = "https://chatgpt.com/"
            headers["Connection"] = "keep-alive"
            
            response = await session.get(url, headers=headers)
            if response.status_code == 200:
                try:
                    data = response.json()
                except Exception:
                    return {"success": False, "error": "无法解析会话 JSON 响应"}
                
                at = data.get("accessToken")
                st = data.get("sessionToken")
                id_token = data.get("idToken") or data.get("id_token")
                if at:
                    return {
                        "success": True,
                        "access_token": at,
                        "session_token": st,
                        "id_token": id_token,
                    }
                
                # 如果 200 但没有 token，可能是被拦截或格式变了
                error_msg = str(data.get("detail") or data.get("error") or "响应中未包含 accessToken")
                return {"success": False, "error": error_msg}
            else:
                error_text = response.text
                try:
                    error_data = response.json()
                    error_msg = error_data.get("detail") or error_data.get("error") or error_text
                    if not isinstance(error_msg, str):
                        error_msg = str(error_msg)
                except:
                    error_msg = error_text
                return {"success": False, "status_code": response.status_code, "error": error_msg}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def refresh_access_token_with_refresh_token(
        self,
        refresh_token: str,
        client_id: str,
        db_session: DBAsyncSession,
        identifier: str = "default"
    ) -> Dict[str, Any]:
        """使用 refresh_token 刷新 AT（兼容多端点/多请求格式）。"""
        if identifier == "default":
            identifier = f"rt_{refresh_token[:8]}"

        # 方案 1：当前主流程（JSON + auth.openai.com）
        primary_url = "https://auth.openai.com/oauth/token"
        primary_payload = {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "redirect_uri": "com.openai.sora://auth.openai.com/android/com.openai.sora/callback",
            "refresh_token": refresh_token
        }
        primary_headers = {"Content-Type": "application/json"}

        result = await self._make_request(
            "POST", primary_url, primary_headers, primary_payload, db_session, identifier
        )
        if result["success"]:
            data = result.get("data", {})
            return {
                "success": True,
                "access_token": data.get("access_token"),
                "id_token": data.get("id_token"),
                "refresh_token": data.get("refresh_token"),
                "data": data
            }

        # 方案 2：回退到 auth0.openai.com + x-www-form-urlencoded
        # 某些 OAuth 客户端（尤其是 app_xxx）在该端点成功率更高。
        fallback_url = "https://auth0.openai.com/oauth/token"
        fallback_form = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
            "scope": "openid profile email offline_access"
        }
        fallback_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json"
        }

        session = await self._get_session(db_session, identifier)
        try:
            response = await session.post(fallback_url, headers=fallback_headers, data=fallback_form)
            if 200 <= response.status_code < 300:
                data = response.json()
                return {
                    "success": True,
                    "access_token": data.get("access_token"),
                    "id_token": data.get("id_token"),
                    "refresh_token": data.get("refresh_token"),
                    "data": data
                }

            # 带上主流程失败信息，便于排查 client_id / rt 不匹配等问题
            fallback_error = response.text
            try:
                fallback_json = response.json()
                detail = fallback_json.get("error_description") or fallback_json.get("error") or fallback_error
                fallback_error = str(detail)
            except Exception:
                pass

            return {
                "success": False,
                "error": (
                    f"refresh_token 刷新失败。primary={result.get('error')} ; "
                    f"fallback_status={response.status_code} fallback_error={fallback_error}"
                ),
                "status_code": response.status_code,
                "error_code": result.get("error_code")
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"refresh_token 刷新异常: {e}; primary={result.get('error')}",
                "error_code": result.get("error_code")
            }

    def create_oauth_authorize_url(
        self,
        client_id: str,
        redirect_uri: str,
        scope: str = "openid email profile offline_access",
        audience: Optional[str] = None,
        codex_cli_simplified_flow: bool = True,
        id_token_add_organizations: bool = True,
    ) -> Dict[str, str]:
        """生成 OpenAI OAuth 授权链接（PKCE）。"""
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("utf-8")).digest()
        ).decode("utf-8").rstrip("=")
        state = secrets.token_urlsafe(24)

        query_dict = {
            "client_id": client_id,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "login",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "state": state,
            "codex_cli_simplified_flow": str(codex_cli_simplified_flow).lower(),
            "id_token_add_organizations": str(id_token_add_organizations).lower(),
        }
        if audience:
            query_dict["audience"] = audience

        query = urlencode(query_dict)
        return {
            "authorize_url": f"https://auth.openai.com/oauth/authorize?{query}",
            "code_verifier": verifier,
            "state": state,
        }

    async def exchange_oauth_code(
        self,
        code: str,
        client_id: str,
        redirect_uri: str,
        code_verifier: str,
        db_session: DBAsyncSession,
        identifier: str = "oauth_exchange"
    ) -> Dict[str, Any]:
        """用 OAuth code 兑换 access_token / refresh_token。"""
        url = "https://auth.openai.com/oauth/token"
        payload = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
        headers = {"Content-Type": "application/json"}

        result = await self._make_request("POST", url, headers, payload, db_session, identifier)
        if not result["success"]:
            return {
                "success": False,
                "error": f"code 换 token 失败: {result.get('error', '未知错误')}"
            }

        data = result.get("data", {})
        return {
            "success": True,
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token"),
            "id_token": data.get("id_token"),
            "data": data,
        }

    async def clear_session(self, identifier: Optional[str] = None):
        """清理指定身份的会话，若不提供则清理所有"""
        if identifier:
            if identifier in self._sessions:
                try:
                    await self._sessions[identifier].close()
                except:
                    pass
                del self._sessions[identifier]
        else:
            await self.close()

    async def close(self):
        """关闭所有会话"""
        for session in self._sessions.values():
            try:
                await session.close()
            except:
                pass
        self._sessions.clear()


# 创建全局实例
chatgpt_service = ChatGPTService()
