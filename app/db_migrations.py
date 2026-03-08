"""
数据库自动迁移模块
在应用启动时自动检测并执行必要的数据库迁移
"""
import logging
import sqlite3
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)


def get_db_path():
    """获取数据库文件路径"""
    from app.config import settings
    db_file = settings.database_url.split("///")[-1]
    return Path(db_file)


def column_exists(cursor, table_name, column_name):
    """检查表中是否存在指定列"""
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [row[1] for row in cursor.fetchall()]
    return column_name in columns


def table_exists(cursor, table_name):
    """检查表是否存在"""
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    )
    return cursor.fetchone() is not None


def run_auto_migration():
    """
    自动运行数据库迁移
    检测缺失的列并自动添加
    """
    db_path = get_db_path()
    
    if not db_path.exists():
        logger.info("数据库文件不存在，跳过迁移")
        return
    
    logger.info("开始检查数据库迁移...")
    
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        
        migrations_applied = []
        
        # 检查并添加质保相关字段
        if not column_exists(cursor, "redemption_codes", "has_warranty"):
            logger.info("添加 redemption_codes.has_warranty 字段")
            cursor.execute("""
                ALTER TABLE redemption_codes 
                ADD COLUMN has_warranty BOOLEAN DEFAULT 0
            """)
            migrations_applied.append("redemption_codes.has_warranty")
        
        if not column_exists(cursor, "redemption_codes", "warranty_expires_at"):
            logger.info("添加 redemption_codes.warranty_expires_at 字段")
            cursor.execute("""
                ALTER TABLE redemption_codes 
                ADD COLUMN warranty_expires_at DATETIME
            """)
            migrations_applied.append("redemption_codes.warranty_expires_at")
        
        if not column_exists(cursor, "redemption_codes", "warranty_days"):
            logger.info("添加 redemption_codes.warranty_days 字段")
            cursor.execute("""
                ALTER TABLE redemption_codes 
                ADD COLUMN warranty_days INTEGER DEFAULT 30
            """)
            migrations_applied.append("redemption_codes.warranty_days")
        
        if not column_exists(cursor, "redemption_records", "is_warranty_redemption"):
            logger.info("添加 redemption_records.is_warranty_redemption 字段")
            cursor.execute("""
                ALTER TABLE redemption_records 
                ADD COLUMN is_warranty_redemption BOOLEAN DEFAULT 0
            """)
            migrations_applied.append("redemption_records.is_warranty_redemption")

        # 检查并添加 Token 刷新相关字段
        if not column_exists(cursor, "teams", "refresh_token_encrypted"):
            logger.info("添加 teams.refresh_token_encrypted 字段")
            cursor.execute("ALTER TABLE teams ADD COLUMN refresh_token_encrypted TEXT")
            migrations_applied.append("teams.refresh_token_encrypted")

        if not column_exists(cursor, "teams", "session_token_encrypted"):
            logger.info("添加 teams.session_token_encrypted 字段")
            cursor.execute("ALTER TABLE teams ADD COLUMN session_token_encrypted TEXT")
            migrations_applied.append("teams.session_token_encrypted")

        if not column_exists(cursor, "teams", "client_id"):
            logger.info("添加 teams.client_id 字段")
            cursor.execute("ALTER TABLE teams ADD COLUMN client_id VARCHAR(100)")
            migrations_applied.append("teams.client_id")

        if not column_exists(cursor, "teams", "error_count"):
            logger.info("添加 teams.error_count 字段")
            cursor.execute("ALTER TABLE teams ADD COLUMN error_count INTEGER DEFAULT 0")
            migrations_applied.append("teams.error_count")

        if not column_exists(cursor, "teams", "account_role"):
            logger.info("添加 teams.account_role 字段")
            cursor.execute("ALTER TABLE teams ADD COLUMN account_role VARCHAR(50)")
            migrations_applied.append("teams.account_role")

        # 生命周期主档与提醒队列
        if not table_exists(cursor, "member_lifecycles"):
            logger.info("创建 member_lifecycles 表")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS member_lifecycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email VARCHAR(255) NOT NULL UNIQUE,
                    first_joined_at DATETIME NOT NULL,
                    policy_type VARCHAR(50) NOT NULL,
                    policy_expires_at DATETIME,
                    has_migration_downtime BOOLEAN DEFAULT 0,
                    is_legacy_seeded BOOLEAN DEFAULT 0,
                    effective_from DATETIME NOT NULL,
                    current_team_id INTEGER,
                    status VARCHAR(20) DEFAULT 'active',
                    created_at DATETIME,
                    updated_at DATETIME,
                    FOREIGN KEY(current_team_id) REFERENCES teams(id)
                )
            """)
            migrations_applied.append("create.member_lifecycles")

        if not table_exists(cursor, "member_lifecycle_events"):
            logger.info("创建 member_lifecycle_events 表")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS member_lifecycle_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lifecycle_id INTEGER NOT NULL,
                    event_type VARCHAR(50) NOT NULL,
                    source_type VARCHAR(20) NOT NULL,
                    code_or_manual_tag VARCHAR(64),
                    has_warranty BOOLEAN DEFAULT 0,
                    warranty_expires_at DATETIME,
                    from_team_id INTEGER,
                    to_team_id INTEGER,
                    event_at DATETIME NOT NULL,
                    meta_json TEXT,
                    FOREIGN KEY(lifecycle_id) REFERENCES member_lifecycles(id) ON DELETE CASCADE
                )
            """)
            migrations_applied.append("create.member_lifecycle_events")

        if not table_exists(cursor, "member_reminder_queue"):
            logger.info("创建 member_reminder_queue 表")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS member_reminder_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lifecycle_id INTEGER NOT NULL,
                    email VARCHAR(255) NOT NULL,
                    policy_type VARCHAR(50) NOT NULL,
                    target_expires_at DATETIME NOT NULL,
                    days_left INTEGER NOT NULL,
                    reason VARCHAR(50) NOT NULL,
                    status VARCHAR(20) DEFAULT 'pending',
                    dedupe_key VARCHAR(255) NOT NULL UNIQUE,
                    last_sent_at DATETIME,
                    last_send_result TEXT,
                    created_at DATETIME,
                    updated_at DATETIME,
                    FOREIGN KEY(lifecycle_id) REFERENCES member_lifecycles(id) ON DELETE CASCADE
                )
            """)
            migrations_applied.append("create.member_reminder_queue")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_lifecycle_policy_expires ON member_lifecycles(policy_expires_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_lifecycle_event_time ON member_lifecycle_events(lifecycle_id, event_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_reminder_status ON member_reminder_queue(status)")
        
        # 提交更改
        conn.commit()
        
        if migrations_applied:
            logger.info(f"数据库迁移完成，应用了 {len(migrations_applied)} 个迁移: {', '.join(migrations_applied)}")
        else:
            logger.info("数据库已是最新版本，无需迁移")
        
        conn.close()
        
    except Exception as e:
        logger.error(f"数据库迁移失败: {e}")
        raise


if __name__ == "__main__":
    # 允许直接运行此脚本进行迁移
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    run_auto_migration()
    print("迁移完成")
