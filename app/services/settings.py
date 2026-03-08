"""
系统设置服务
管理系统配置的读取、更新和缓存
"""
from typing import Optional, Dict, Any
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Setting
from app.config import settings
import logging

logger = logging.getLogger(__name__)


class SettingsService:
    """系统设置服务类"""

    def __init__(self):
        self._cache: Dict[str, str] = {}

    async def get_setting(self, session: AsyncSession, key: str, default: Optional[str] = None) -> Optional[str]:
        """
        获取单个配置项

        Args:
            session: 数据库会话
            key: 配置项键名
            default: 默认值

        Returns:
            配置项值,如果不存在则返回默认值
        """
        # 先从缓存获取
        if key in self._cache:
            return self._cache[key]

        # 从数据库获取
        result = await session.execute(
            select(Setting).where(Setting.key == key)
        )
        setting = result.scalar_one_or_none()

        if setting:
            self._cache[key] = setting.value
            return setting.value

        return default

    async def get_all_settings(self, session: AsyncSession) -> Dict[str, str]:
        """
        获取所有配置项

        Args:
            session: 数据库会话

        Returns:
            配置项字典
        """
        result = await session.execute(select(Setting))
        settings = result.scalars().all()

        settings_dict = {s.key: s.value for s in settings}
        self._cache.update(settings_dict)

        return settings_dict

    async def update_setting(self, session: AsyncSession, key: str, value: str) -> bool:
        """
        更新单个配置项

        Args:
            session: 数据库会话
            key: 配置项键名
            value: 配置项值

        Returns:
            是否更新成功
        """
        try:
            result = await session.execute(
                select(Setting).where(Setting.key == key)
            )
            setting = result.scalar_one_or_none()

            if setting:
                setting.value = value
            else:
                setting = Setting(key=key, value=value)
                session.add(setting)

            await session.commit()

            # 更新缓存
            self._cache[key] = value

            logger.info(f"配置项 {key} 已更新")
            return True

        except Exception as e:
            logger.error(f"更新配置项 {key} 失败: {e}")
            await session.rollback()
            return False

    async def update_settings(self, session: AsyncSession, settings: Dict[str, str]) -> bool:
        """
        批量更新配置项

        Args:
            session: 数据库会话
            settings: 配置项字典

        Returns:
            是否更新成功
        """
        try:
            for key, value in settings.items():
                result = await session.execute(
                    select(Setting).where(Setting.key == key)
                )
                setting = result.scalar_one_or_none()

                if setting:
                    setting.value = value
                else:
                    setting = Setting(key=key, value=value)
                    session.add(setting)

            await session.commit()

            # 更新缓存
            self._cache.update(settings)

            logger.info(f"批量更新了 {len(settings)} 个配置项")
            return True

        except Exception as e:
            logger.error(f"批量更新配置项失败: {e}")
            await session.rollback()
            return False

    def clear_cache(self):
        """清空缓存"""
        self._cache.clear()
        logger.info("配置缓存已清空")

    async def get_proxy_config(self, session: AsyncSession) -> Dict[str, str]:
        """
        获取代理配置

        Returns:
            代理配置字典
        """
        proxy_enabled = await self.get_setting(session, "proxy_enabled", "false")
        proxy = await self.get_setting(session, "proxy", "")

        return {
            "enabled": str(proxy_enabled).lower() == "true",
            "proxy": proxy
        }

    async def update_proxy_config(
        self,
        session: AsyncSession,
        enabled: bool,
        proxy: str = ""
    ) -> bool:
        """
        更新代理配置

        Args:
            session: 数据库会话
            enabled: 是否启用代理
            proxy: 代理地址 (格式: http://host:port 或 socks5://host:port)

        Returns:
            是否更新成功
        """
        settings = {
            "proxy_enabled": str(enabled).lower(),
            "proxy": proxy
        }

        return await self.update_settings(session, settings)


    async def get_token_auto_refresh_config(self, session: AsyncSession) -> Dict[str, int]:
        """
        获取 Token 自动刷新配置

        Returns:
            Token 自动刷新配置
        """
        enabled_raw = await self.get_setting(
            session,
            "token_auto_refresh_enabled",
            str(settings.token_auto_refresh_enabled).lower()
        )
        interval_raw = await self.get_setting(
            session,
            "token_auto_refresh_interval_seconds",
            str(settings.token_auto_refresh_interval_seconds)
        )
        lead_raw = await self.get_setting(
            session,
            "token_refresh_lead_seconds",
            str(settings.token_refresh_lead_seconds)
        )

        try:
            interval = max(5, int(interval_raw))
        except Exception:
            interval = max(5, int(settings.token_auto_refresh_interval_seconds))

        try:
            lead_seconds = max(0, int(lead_raw))
        except Exception:
            lead_seconds = max(0, int(settings.token_refresh_lead_seconds))

        return {
            "enabled": str(enabled_raw).lower() == "true",
            "interval_seconds": interval,
            "lead_seconds": lead_seconds
        }

    async def update_token_auto_refresh_config(
        self,
        session: AsyncSession,
        enabled: bool,
        interval_seconds: int,
        lead_seconds: int
    ) -> bool:
        """
        更新 Token 自动刷新配置
        """
        normalized_interval = max(5, int(interval_seconds))
        normalized_lead = max(0, int(lead_seconds))

        updated = await self.update_settings(
            session,
            {
                "token_auto_refresh_enabled": str(enabled).lower(),
                "token_auto_refresh_interval_seconds": str(normalized_interval),
                "token_refresh_lead_seconds": str(normalized_lead)
            }
        )
        if not updated:
            return False

        # 同步更新运行时配置（无需重启）
        settings.token_auto_refresh_enabled = enabled
        settings.token_auto_refresh_interval_seconds = normalized_interval
        settings.token_refresh_lead_seconds = normalized_lead
        return True

    async def get_log_level(self, session: AsyncSession) -> str:
        """
        获取日志级别

        Returns:
            日志级别
        """
        return await self.get_setting(session, "log_level", "INFO")

    async def update_log_level(self, session: AsyncSession, level: str) -> bool:
        """
        更新日志级别

        Args:
            session: 数据库会话
            level: 日志级别 (DEBUG/INFO/WARNING/ERROR/CRITICAL)

        Returns:
            是否更新成功
        """
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if level.upper() not in valid_levels:
            logger.error(f"无效的日志级别: {level}")
            return False

        success = await self.update_setting(session, "log_level", level.upper())

        if success:
            # 动态更新日志级别
            logging.getLogger().setLevel(level.upper())
            logger.info(f"日志级别已更新为: {level.upper()}")

        return success


    async def get_reminder_email_config(self, session: AsyncSession) -> Dict[str, Any]:
        """获取到期提醒邮件配置（仅用于手动发件模板）"""
        due_days_raw = await self.get_setting(session, "reminder_due_days", "3")

        try:
            due_days = max(0, int(due_days_raw))
        except Exception:
            due_days = 3

        return {
            "due_days": due_days,
            "subject": await self.get_setting(session, "reminder_email_subject", "team空间到期提醒"),
            "body_template": await self.get_setting(
                session,
                "reminder_email_body",
                "您好，您加入的team工作空间一个月套餐即将到期，请及时联系管理员续期，否则到期后将踢出工作空间~"
            ),
        }

    async def update_reminder_email_config(self, session: AsyncSession, data: Dict[str, Any]) -> bool:
        """更新到期提醒邮件配置（仅保存提醒规则和邮件模板）"""
        normalized_due_days = max(0, int(data.get("due_days", 3)))
        settings_payload = {
            "reminder_due_days": str(normalized_due_days),
            "reminder_email_subject": data.get("subject", "team空间到期提醒"),
            "reminder_email_body": data.get("body_template", ""),
        }
        return await self.update_settings(session, settings_payload)

# 创建全局实例
settings_service = SettingsService()
