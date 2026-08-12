"""
数据库连接模块
SQLite 异步连接配置和会话管理
"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from app.config import settings

# SQLite 本质上是单写者数据库，即便开启 WAL，过大的连接池反而会放大
# "database is locked" 的概率。对 SQLite 使用较小的池；对真正支持高并发的
# 后端（MySQL / PostgreSQL）才放开到原来的大池子。
_is_sqlite = settings.database_url.startswith("sqlite")

if _is_sqlite:
    _engine_kwargs = dict(
        connect_args={"timeout": 60},
        pool_size=5,
        max_overflow=10,
        pool_recycle=3600,
        pool_pre_ping=True,
    )
else:
    _engine_kwargs = dict(
        pool_size=50,
        max_overflow=100,
        pool_recycle=3600,
        pool_pre_ping=True,
    )

# 创建异步引擎
engine = create_async_engine(
    settings.database_url,
    echo=settings.database_echo,  # 控制是否打印 SQL
    future=True,
    **_engine_kwargs,
)

# 创建异步会话工厂
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False
)

# 创建 Base 类
Base = declarative_base()


async def get_db() -> AsyncSession:
    """
    获取数据库会话
    用于 FastAPI 依赖注入
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    """
    初始化数据库
    创建所有表
    """
    async with engine.begin() as conn:
        # WAL 是 SQLite 专用设置；PostgreSQL/MySQL 等后端执行该语句会导致启动失败。
        if _is_sqlite:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(Base.metadata.create_all)


async def close_db():
    """
    关闭数据库连接
    """
    await engine.dispose()
