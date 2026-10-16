'''db/base.py'''

"""
ORM 基类

单独一个文件，避免 db/session.py（引擎/会话）与 db/models.py（模型）
互相 import 形成环。
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型的基类（SQLAlchemy 2.0 声明式风格）"""

    def to_dict(self, exclude: tuple = ()) -> dict:
        """
        【便捷方法】把模型实例转成字典，便于直接塞进图状态或返回 JSON。
        :param exclude: 需要排除的字段名
        :return: 字段名 → 值 的字典
        """
        return {
            column.name: getattr(self, column.name)
            for column in self.__table__.columns
            if column.name not in exclude
        }

    def __repr__(self) -> str:
        """统一的可读表示：类名 + 主键"""
        primary_key = getattr(self, 'id', None)
        return f'<{self.__class__.__name__} id={primary_key}>'
