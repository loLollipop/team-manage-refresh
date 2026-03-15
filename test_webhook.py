import asyncio

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.services.notification import notification_service
from app.services.settings import settings_service

# 这是一个手动联调脚本，不应作为默认自动化测试在 CI 中执行
pytestmark = pytest.mark.skip(reason="manual integration test")

# 建议在开发环境下运行，它会修改数据库中的配置
DATABASE_URL = "sqlite+aiosqlite:///./team_manage.db"
engine = create_async_engine(DATABASE_URL, echo=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _run_webhook_check():
    async with AsyncSessionLocal() as db:
        # 1. 设置 Webhook URL
        test_url = "https://webhook.site/placeholder"
        await settings_service.update_settings(db, {
            "webhook_url": test_url,
            "low_stock_threshold": "100"  # 设高一点确保触发
        })

        print(f"Checking stock level (seats & codes) and sending webhook to {test_url}...")

    # 2. 手动触发检查 (不需要传 db_session 了，它内部会创建)
    result = await notification_service.check_and_notify_low_stock()

    if result:
        print("Webhook check triggered notification successfully (check logs).")
    else:
        print("Webhook notification was not sent (maybe stock is higher than threshold or error occurred).")


def test_webhook():
    """保留为 pytest 测试入口，但默认跳过。"""
    asyncio.run(_run_webhook_check())


if __name__ == "__main__":
    asyncio.run(_run_webhook_check())
