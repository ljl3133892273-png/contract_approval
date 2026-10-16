'''db/session.py'''

"""
数据库引擎与会话管理

职责:
    1. 按配置创建引擎（SQLite / MySQL / PostgreSQL 参数分支）
    2. 提供会话工厂与事务性上下文管理器
    3. 建表 / 删表

关于 SQLite 的两个坑（本项目必须处理）:
    1. FastAPI 的 BackgroundTasks 跑在**工作线程**里，而 SQLite 连接默认禁止跨线程复用，
       必须加 connect_args={'check_same_thread': False}，否则后台任务一写库就报错。
    2. SQLite 默认**不强制外键约束**，必须每条连接建立时执行 PRAGMA foreign_keys=ON，
       否则脏数据（指向不存在任务的条款）能照常插进去。
"""

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from config_bag.db_config import get_db_config
from db.base import Base

# 引擎与会话工厂（懒加载单例）
_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker] = None


def _attach_sqlite_pragmas(engine: Engine) -> None:
    """
    【辅助函数】为 SQLite 引擎挂上连接级 PRAGMA
    功能：每条新连接都打开外键约束（SQLite 默认关闭）
    """
    @event.listens_for(engine, 'connect')
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute('PRAGMA foreign_keys=ON')
        cursor.close()


def _build_engine(url: str, echo: bool) -> Engine:
    """
    【辅助函数】按 URL 创建引擎
    :param url: 连接串
    :param echo: 是否打印 SQL
    :return: Engine 实例
    """
    config = get_db_config()

    if url.startswith('sqlite'):
        # 文件型 SQLite 必须先保证父目录存在，否则连接直接失败。
        # 注意：这里必须从**传入的 url** 推导路径，而不是从配置里取 ——
        # 否则测试传入临时库地址时，建的还是 .env 里那个目录。
        raw_path = url.split('://', 1)[-1]
        if raw_path and not raw_path.startswith(':memory:'):
            Path(raw_path.lstrip('/')).parent.mkdir(parents=True, exist_ok=True)

        engine = create_engine(
            url,
            echo=echo,
            # 允许跨线程复用连接（BackgroundTasks 在工作线程里写库）
            connect_args={'check_same_thread': False},
        )
        _attach_sqlite_pragmas(engine)
        return engine

    # 服务型数据库（MySQL / PostgreSQL）：启用连接池参数
    return create_engine(
        url,
        echo=echo,
        pool_size=config.pool_size,
        max_overflow=config.max_overflow,
        pool_pre_ping=config.pool_pre_ping,
    )


def get_engine() -> Engine:
    """获取引擎单例（懒加载，从 .env 读配置）"""
    global _engine
    if _engine is None:
        config = get_db_config()
        _engine = _build_engine(config.url, config.echo)
    return _engine


def init_engine(url: str, echo: bool = False) -> Engine:
    """
    显式重建引擎（应用启动或测试指定连接串时使用）
    :param url: 连接串
    :param echo: 是否打印 SQL
    :return: 新的 Engine 实例
    """
    global _engine, _session_factory
    _engine = _build_engine(url, echo)
    _session_factory = None  # 会话工厂要跟着引擎一起重建
    return _engine


def reset_engine() -> None:
    """清空引擎与会话工厂单例（测试清理用）"""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def get_session_factory() -> sessionmaker:
    """获取会话工厂单例"""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,  # 提交后仍可读对象属性，方便返回给上层
        )
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """
    事务性会话上下文管理器（推荐用法）

    正常结束自动 commit，抛异常自动 rollback，最后一定 close。
    用法：
        with session_scope() as session:
            session.add(task)
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """
    建表（演示用 create_all）。
    生产建议引入 Alembic 做版本化迁移，此处从简。
    """
    # 必须先 import models，让模型注册到 Base.metadata 上，否则建出来是空库
    from db import models  # noqa: F401

    Base.metadata.create_all(bind=get_engine())


def drop_all() -> None:
    """删除全部表（仅测试用）"""
    from db import models  # noqa: F401

    Base.metadata.drop_all(bind=get_engine())


if __name__ == '__main__':
    # 轻量自测：引擎可建、SQLite 参数分支正确、重复获取返回同一实例
    import tempfile
    from pathlib import Path

    temp_dir = Path(tempfile.mkdtemp(prefix='db_session_test_'))
    test_url = f'sqlite:///{temp_dir.as_posix()}/session_test.db'

    reset_engine()
    engine = init_engine(test_url)

    assert engine.dialect.name == 'sqlite', '应使用 sqlite 方言'
    assert get_engine() is engine, 'get_engine 应返回同一个引擎实例'

    # 验证外键 PRAGMA 真的生效了（这是 SQLite 上最容易漏的一步）
    with engine.connect() as connection:
        foreign_keys_enabled = connection.exec_driver_sql('PRAGMA foreign_keys').scalar()
    assert foreign_keys_enabled == 1, 'SQLite 外键约束未打开'

    # 验证会话上下文管理器可用（能真正执行 SQL）
    from sqlalchemy import text

    with session_scope() as session:
        assert session.execute(text('SELECT 1')).scalar() == 1, '会话无法执行 SQL'
        # 外键 PRAGMA 必须对「会话取到的连接」同样生效
        assert session.execute(text('PRAGMA foreign_keys')).scalar() == 1, \
            '会话连接上外键约束未打开'

    # 验证会话工厂在 reset 后会跟着引擎一起重建
    old_factory = get_session_factory()
    reset_engine()
    assert get_session_factory() is not old_factory, 'reset_engine 后会话工厂应重建'

    reset_engine()
    print('db.session 自测通过 ✅（引擎/外键/会话均正常）')
