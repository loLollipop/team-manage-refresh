import logging
import httpx
import asyncio
from typing import Optional, Any, Dict
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.settings import settings_service
from app.services.redemption import RedemptionService
from app.services.team import team_service
from app.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

class NotificationService:
    """通知服务类"""

    def __init__(self):
        self.redemption_service = RedemptionService()

    async def check_and_notify_low_stock(self) -> bool:
        """
        检查库存（车位）并发送通知
        使用独立的数据库会话以支持异步后台任务
        """
        async with AsyncSessionLocal() as db_session:
            try:
                # 1. 获取配置
                webhook_url = await settings_service.get_setting(db_session, "webhook_url")
                if not webhook_url:
                    return False

                threshold_str = await settings_service.get_setting(db_session, "low_stock_threshold", "10")
                api_key = await settings_service.get_setting(db_session, "api_key")

                try:
                    threshold = int(threshold_str)
                except (ValueError, TypeError):
                    threshold = 10

                # 2. 检查可用车位 (作为预警指标)
                available_seats = await team_service.get_total_available_seats(db_session)
                
                logger.info(f"库存检查 - 当前总可用车位: {available_seats}, 触发阈值: {threshold}")

                # 仅根据可用车位触发补货
                if available_seats <= threshold:
                    logger.info(f"检测到车位不足，触发补货预警! Webhook URL: {webhook_url}")
                    return await self.send_webhook_notification(webhook_url, available_seats, threshold, api_key)
                
                return False

            except Exception as e:
                logger.error(f"检查库存并通知过程发生错误: {e}")
                return False

    async def send_webhook_notification(self, url: str, available_seats: int, threshold: int, api_key: Optional[str] = None) -> bool:
        """
        发送 Webhook 通知
        """
        try:
            payload = {
                "event": "low_stock",
                "current_seats": available_seats,
                "threshold": threshold,
                "message": f"库存不足预警：系统总可用车位仅剩 {available_seats}，已低于预警阈值 {threshold}，请及时补货导入新账号。"
            }
            
            headers = {}
            if api_key:
                headers["X-API-Key"] = api_key
                
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                logger.info(f"Webhook 通知发送成功: {url}")
                return True
        except Exception as e:
            logger.error(f"发送 Webhook 通知失败: {e}")
            return False

# 创建全局实例
notification_service = NotificationService()
