from typing import Dict, Optional
from urllib.parse import urlparse, urlunparse

SUPPORTED_PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")


def normalize_proxy_url(proxy: Optional[str]) -> Optional[str]:
    value = str(proxy or "").strip()
    if not value:
        return None

    parsed = urlparse(value)
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(
            "代理地址格式错误,应为 http://host:port, https://host:port, socks5://host:port 或 socks5h://host:port"
        ) from exc

    if parsed.scheme not in SUPPORTED_PROXY_SCHEMES or not parsed.netloc or not parsed.hostname:
        raise ValueError(
            "代理地址格式错误,应为 http://host:port, https://host:port, socks5://host:port 或 socks5h://host:port"
        )

    return value


def build_curl_cffi_proxies(proxy: Optional[str]) -> Optional[Dict[str, str]]:
    normalized_proxy = normalize_proxy_url(proxy)
    if not normalized_proxy:
        return None

    return {
        "all": normalized_proxy,
        "http": normalized_proxy,
        "https": normalized_proxy,
    }


def build_httpx_proxy(proxy: Optional[str]) -> Optional[str]:
    return normalize_proxy_url(proxy)


def mask_proxy_url(proxy: Optional[str]) -> str:
    normalized_proxy = normalize_proxy_url(proxy)
    if not normalized_proxy:
        return ""

    parsed = urlparse(normalized_proxy)
    netloc = parsed.netloc
    if parsed.username is not None:
        credentials = "***"
        if parsed.password is not None:
            credentials = f"{credentials}:***"
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        netloc = f"{credentials}@{host}"

    return urlunparse(parsed._replace(netloc=netloc))
