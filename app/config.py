"""
应用配置模块
使用 Pydantic Settings 管理配置
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path


# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """应用配置"""

    # 应用配置
    app_name: str = "GPT Team 管理系统"
    app_version: str = "0.1.0"
    app_host: str = "0.0.0.0"
    app_port: int = 8008
    debug: bool = False

    # 数据库配置
    # 建议在 Docker 中使用 data 目录挂载，以避免文件挂载权限或类型问题
    database_url: str = f"sqlite+aiosqlite:///{BASE_DIR}/data/team_manage.db"

    # 安全配置
    # secret_key 仍保留作为向后兼容的统一密钥（未显式配置 session/encryption 时回退）。
    # 但强烈建议在生产环境分别配置 SESSION_SECRET_KEY 与 ENCRYPTION_KEY，
    # 以便在只需要轮换 Session 密钥时不会导致历史 Token 无法解密。
    secret_key: str = "your-secret-key-here-change-in-production"
    session_secret_key: str = ""
    encryption_key: str = ""
    admin_password: str = "admin123"
    # Cookie 是否仅允许 HTTPS (生产环境应设为 True)
    session_cookie_secure: bool = False

    # 日志配置
    log_level: str = "INFO"
    database_echo: bool = False

    # 代理配置
    proxy: str = ""
    proxy_enabled: bool = False

    # JWT 配置
    jwt_verify_signature: bool = False

    # 时区配置
    timezone: str = "Asia/Shanghai"

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False
    )

    @property
    def effective_session_secret_key(self) -> str:
        return self.session_secret_key or self.secret_key

    @property
    def effective_encryption_key(self) -> str:
        return self.encryption_key or self.secret_key


# 创建全局配置实例
settings = Settings()
