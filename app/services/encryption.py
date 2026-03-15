"""
加密服务
用于加密和解密敏感信息 (如 AT Token)
"""
import logging
from cryptography.fernet import Fernet
from app.config import settings
import base64
import hashlib

logger = logging.getLogger(__name__)


class EncryptionService:
    """加密服务类"""

    def __init__(self):
        """初始化加密服务"""
        # 从配置中获取密钥,并转换为 Fernet 兼容的格式
        self._fernet = self._create_fernet()

    def _create_fernet(self) -> Fernet:
        """
        创建 Fernet 加密器

        Returns:
            Fernet 实例
        """
        # 使用 secret_key 生成 Fernet 密钥 (32 字节 base64 编码)
        key_bytes = settings.secret_key.encode('utf-8')
        # 使用 SHA256 哈希生成固定长度的密钥
        hashed_key = hashlib.sha256(key_bytes).digest()
        # Base64 编码
        fernet_key = base64.urlsafe_b64encode(hashed_key)
        return Fernet(fernet_key)

    def encrypt_token(self, token: str) -> str:
        """
        加密 Token

        Args:
            token: 原始 Token 字符串

        Returns:
            加密后的 Token (Base64 编码字符串)
        """
        try:
            token_bytes = token.encode('utf-8')
            encrypted_bytes = self._fernet.encrypt(token_bytes)
            encrypted_str = encrypted_bytes.decode('utf-8')
            logger.debug("Token 加密成功")
            return encrypted_str
        except Exception as e:
            logger.error(f"Token 加密失败: {e}")
            raise

    def decrypt_token(self, encrypted_token: str) -> str:
        """
        解密 Token

        Args:
            encrypted_token: 加密的 Token 字符串

        Returns:
            原始 Token 字符串
        """
        try:
            encrypted_bytes = encrypted_token.encode('utf-8')
            decrypted_bytes = self._fernet.decrypt(encrypted_bytes)
            token = decrypted_bytes.decode('utf-8')
            logger.debug("Token 解密成功")
            return token
        except Exception as e:
            logger.error(f"Token 解密失败: {e}")
            raise


# 创建全局加密服务实例
encryption_service = EncryptionService()
