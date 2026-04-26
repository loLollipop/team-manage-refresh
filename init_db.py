"""
数据库初始化脚本
创建所有表并插入默认数据
"""
import asyncio
import bcrypt
from sqlalchemy import select
from app.database import init_db, AsyncSessionLocal
from app.models import Setting
from app.config import settings


async def create_default_settings():
    """创建默认系统设置"""
    async with AsyncSessionLocal() as session:
        # 检查是否已经初始化
        result = await session.execute(select(Setting).where(Setting.key == "initialized"))
        existing = result.scalar_one_or_none()

        if existing:
            print("数据库已经初始化,跳过默认数据插入")
            return

        # 生成管理员密码哈希
        password_hash = bcrypt.hashpw(
            settings.admin_password.encode('utf-8'),
            bcrypt.gensalt()
        ).decode('utf-8')

        # 默认设置
        default_settings = [
            Setting(
                key="initialized",
                value="true",
                description="数据库初始化标记"
            ),
            Setting(
                key="admin_password_hash",
                value=password_hash,
                description="管理员密码哈希"
            ),
            Setting(
                key="proxy",
                value=settings.proxy,
                description="代理地址 (支持 http://、https://、socks5:// 和 socks5h://)"
            ),
            Setting(
                key="proxy_enabled",
                value=str(settings.proxy_enabled).lower(),
                description="是否启用代理"
            ),
            Setting(
                key="log_level",
                value=settings.log_level,
                description="日志级别"
            ),
            Setting(
                key="default_team_max_members",
                value="6",
                description="新导入 Team 的默认总席位"
            ),
            Setting(
                key="warranty_expiration_mode",
                value="first_use",
                description="质保时长计算模式: first_use/refresh_on_redeem"
            ),
            Setting(
                key="warranty_auto_kick_enabled",
                value="false",
                description="是否启用质保过期自动踢人"
            ),
            Setting(
                key="warranty_auto_kick_interval_hours",
                value="12",
                description="质保过期自动踢人检查间隔（小时）"
            ),
            Setting(
                key="warranty_renewal_reminder_days",
                value="7",
                description="距离质保结束多少天内提醒用户联系管理员续期"
            ),
            Setting(
                key="auto_kick_usage_period_days",
                value="30",
                description="无质保兑换码的使用期限（天）；自动踢人按该期限判定无质保码是否到期"
            ),
        ]

        session.add_all(default_settings)
        await session.commit()
        print("默认设置已创建")


async def main():
    """主函数"""
    print("开始初始化数据库...")

    # 创建所有表
    await init_db()
    print("数据库表创建完成")

    # 插入默认数据
    await create_default_settings()

    print("数据库初始化完成!")


if __name__ == "__main__":
    asyncio.run(main())
