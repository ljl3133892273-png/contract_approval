'''config_bag/db_config.py'''

"""
数据库配置

从 .env 读 DATABASE_URL，统一管理引擎与会话参数。
演示环境用 SQLite，生产切 MySQL 8 / PostgreSQL 15（SPEC 3、SPEC 6）。

注意:
    本模块只负责「连接参数」，不负责建引擎 —— 建引擎在 db/session.py，
    因为 SQLite 与 MySQL/PG 的引擎参数差异（连接池、check_same_thread）必须在那里分支处理。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import os

from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv(override=True)

# 缺省连接串：演示用 SQLite
DEFAULT_DATABASE_URL = 'sqlite:///./data/app.db'


def _env_bool(name: str, default: bool) -> bool:
    """读布尔型环境变量；只有显式写成 false/0/no 才算关闭"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ('false', '0', 'no')


@dataclass
class DatabaseConfig:
    """数据库配置"""

    # ==================== 连接 ====================
    url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "").strip() or DEFAULT_DATABASE_URL
    )
    # 是否打印 SQL（排查问题时开）
    echo: bool = field(default_factory=lambda: _env_bool("DATABASE_ECHO", False))

    # ==================== 连接池（仅服务型数据库生效）====================
    pool_size: int = 5
    max_overflow: int = 10
    # 取出连接前先探活，避免用到已被服务端断开的连接
    pool_pre_ping: bool = True

    @classmethod
    def from_env(cls) -> "DatabaseConfig":
        """从环境变量加载配置"""
        return cls()

    # ==================== 派生属性 ====================
    @property
    def is_sqlite(self) -> bool:
        """是否 SQLite（决定引擎参数分支）"""
        return self.url.startswith('sqlite')

    @property
    def sqlite_file_path(self) -> Optional[Path]:
        """
        SQLite 的库文件路径。
        内存库（sqlite:///:memory: 或 sqlite://）返回 None —— 内存库不需要建目录。
        """
        if not self.is_sqlite:
            return None

        # 去掉 sqlite:// 前缀（含 sqlite:/// 与 sqlite+pysqlite:/// 两种写法）
        raw_path = self.url.split('://', 1)[-1]
        if not raw_path or ':memory:' in raw_path:
            return None
        # sqlite:///./data/app.db → raw_path 为 /./data/app.db，去掉前导斜杠
        return Path(raw_path.lstrip('/'))

    @property
    def dialect_name(self) -> str:
        """方言名（sqlite / mysql / postgresql），用于日志"""
        return self.url.split(':', 1)[0].split('+', 1)[0]


# ==================== 全局单例 ====================
_db_config: DatabaseConfig = DatabaseConfig.from_env()


def get_db_config() -> DatabaseConfig:
    """获取数据库配置单例"""
    return _db_config


if __name__ == '__main__':
    # 轻量自测
    config = get_db_config()

    assert config.dialect_name == 'sqlite', f'演示环境应为 sqlite，实际 {config.dialect_name}'
    assert config.is_sqlite is True
    assert config.sqlite_file_path is not None, '文件型 SQLite 应能解析出路径'
    assert config.sqlite_file_path.name == 'app.db', \
        f'应解析出 app.db，实际 {config.sqlite_file_path}'

    # 内存库不应解析出文件路径
    memory_config = DatabaseConfig(url='sqlite:///:memory:')
    assert memory_config.sqlite_file_path is None, '内存库不应解析出文件路径'

    # 服务型数据库
    mysql_config = DatabaseConfig(url='mysql+pymysql://u:p@127.0.0.1:3306/db')
    assert mysql_config.is_sqlite is False
    assert mysql_config.dialect_name == 'mysql', f'方言名解析错误：{mysql_config.dialect_name}'

    print('db_config 自测通过 ✅')
