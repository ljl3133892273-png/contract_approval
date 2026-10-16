'''db/models.py'''

"""
ORM 模型（对应 SPEC 第 6 章「数据模型与数据库设计」）

共 10 张表：
    contract_task / contract_meta / clause / risk_item / rule /
    template_clause / review_comment / task_event_log / report / app_user

两点实现说明:
    1. 主键用 BIGINT，但 SQLite 只对 INTEGER PRIMARY KEY 自增，
       所以用 `BigInteger().with_variant(Integer, 'sqlite')` 做方言适配。
    2. SPEC 里写 TINYINT 的布尔字段（enabled），这里用 Boolean ——
       语义更明确，SQLAlchemy 会自动映射成 SQLite 的 INTEGER。
"""

from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CHAR,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from db.constants import (
    AdoptStatus,
    ClauseType,
    ReportFormat,
    ReviewStatus,
    RiskLevel,
    RiskSource,
    SourceType,
    UserRole,
    WritebackStatus,
)

# 主键 / 外键类型：SQLite 上退化为 INTEGER 才能自增
ID_TYPE = BigInteger().with_variant(Integer, 'sqlite')


# ============================================================
# 1. contract_task —— 审查任务
# ============================================================
class ContractTask(Base):
    """审查任务（SPEC 6.2）"""

    __tablename__ = 'contract_task'

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)

    # 审批单号：既是回写幂等键，也是附件去重键
    # 注意：上传来源请存 NULL 而不是空字符串 —— 唯一索引下多个 NULL 允许共存，多个 '' 会冲突
    external_doc_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, unique=True,
        comment='审批单/单据号（幂等键）；上传来源请存 NULL'
    )

    contract_name: Mapped[str] = mapped_column(String(255), nullable=False, default='')
    applicant: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    department: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    biz_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # 采购/销售/服务/劳动

    source_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SourceType.UPLOAD
    )
    file_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    file_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # docx/pdf/image
    file_hash: Mapped[Optional[str]] = mapped_column(CHAR(64), nullable=True)  # SHA256，去重用

    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)

    review_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ReviewStatus.PENDING
    )
    writeback_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=WritebackStatus.NOT_WRITTEN
    )
    risk_level: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)

    blocked_reason: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    # ==================== 关系 ====================
    meta: Mapped[Optional["ContractMeta"]] = relationship(
        back_populates='task', uselist=False, cascade='all, delete-orphan'
    )
    clauses: Mapped[List["Clause"]] = relationship(
        back_populates='task', cascade='all, delete-orphan'
    )
    risk_items: Mapped[List["RiskItem"]] = relationship(
        back_populates='task', cascade='all, delete-orphan'
    )
    comment: Mapped[Optional["ReviewComment"]] = relationship(
        back_populates='task', uselist=False, cascade='all, delete-orphan'
    )
    event_logs: Mapped[List["TaskEventLog"]] = relationship(
        back_populates='task', cascade='all, delete-orphan'
    )
    reports: Mapped[List["Report"]] = relationship(
        back_populates='task', cascade='all, delete-orphan'
    )

    # ==================== 索引（SPEC 6.3）====================
    __table_args__ = (
        Index('ix_task_hash_doc', 'file_hash', 'external_doc_id'),  # 附件去重查询
    )


# ============================================================
# 2. contract_meta —— 合同元数据（1:1）
# ============================================================
class ContractMeta(Base):
    """合同元数据（SPEC 6.2）"""

    __tablename__ = 'contract_meta'

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey('contract_task.id', ondelete='CASCADE'), nullable=False, unique=True
    )

    contract_no: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    party_a: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # 我方
    party_b: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # 相对方
    credit_code: Mapped[Optional[str]] = mapped_column(CHAR(18), nullable=True)

    # 冗余金额与币种，便于列表页直接查询而不必 join contract_task
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)

    period_start: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    period_end: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    effective_condition: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # 原始提取全量 JSON（含各字段的 Locator，供工作台「元数据高亮」）
    raw_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    task: Mapped["ContractTask"] = relationship(back_populates='meta')


# ============================================================
# 3. clause —— 条款（含 Locator）
# ============================================================
class Clause(Base):
    """条款（SPEC 6.2）"""

    __tablename__ = 'clause'

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey('contract_task.id', ondelete='CASCADE'), nullable=False
    )

    clause_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ClauseType.OTHER
    )
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default='')

    # ==================== Locator（SPEC 4.2.4）====================
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    page_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    paragraph_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    task: Mapped["ContractTask"] = relationship(back_populates='clauses')
    risk_items: Mapped[List["RiskItem"]] = relationship(
        back_populates='clause', cascade='all, delete-orphan'
    )

    __table_args__ = (
        Index('ix_clause_task_seq', 'task_id', 'seq'),
    )


# ============================================================
# 4. risk_item —— 风险点
# ============================================================
class RiskItem(Base):
    """风险点（SPEC 6.2）"""

    __tablename__ = 'risk_item'

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey('contract_task.id', ondelete='CASCADE'), nullable=False
    )
    # 条款可空：主体缺失这类风险是「整份合同」层面的，挂不到具体条款上
    clause_id: Mapped[Optional[int]] = mapped_column(
        ID_TYPE, ForeignKey('clause.id', ondelete='CASCADE'), nullable=True
    )

    dimension: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    risk_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    risk_level: Mapped[str] = mapped_column(String(8), nullable=False, default=RiskLevel.NONE)

    cause: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)  # 风险成因
    legal_basis: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)  # 合规依据
    suggestion_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # 推荐修改条款

    matched_rule_id: Mapped[Optional[int]] = mapped_column(
        ID_TYPE, ForeignKey('rule.id', ondelete='SET NULL'), nullable=True
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False, default=RiskSource.RULE)

    adopt_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AdoptStatus.PENDING
    )
    manual_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # 法务批注

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    task: Mapped["ContractTask"] = relationship(back_populates='risk_items')
    clause: Mapped[Optional["Clause"]] = relationship(back_populates='risk_items')
    matched_rule: Mapped[Optional["Rule"]] = relationship(back_populates='risk_items')

    __table_args__ = (
        Index('ix_risk_task_level', 'task_id', 'risk_level'),
    )


# ============================================================
# 5. rule —— 审查规则
# ============================================================
class Rule(Base):
    """审查规则（SPEC 6.2、9.2）"""

    __tablename__ = 'rule'

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)  # R-01 ...
    contract_type: Mapped[str] = mapped_column(String(32), nullable=False, default='*')  # * = 通用
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    dimension: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # 触发条件表达式（SPEC 9.2 的结构化 JSON）
    trigger_condition: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    risk_level: Mapped[str] = mapped_column(String(8), nullable=False, default=RiskLevel.MID)
    legal_basis: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    template_clause_id: Mapped[Optional[int]] = mapped_column(
        ID_TYPE, ForeignKey('template_clause.id', ondelete='SET NULL'), nullable=True
    )

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    risk_items: Mapped[List["RiskItem"]] = relationship(back_populates='matched_rule')
    template_clause: Mapped[Optional["TemplateClause"]] = relationship(
        back_populates='rules', foreign_keys=[template_clause_id]
    )


# ============================================================
# 6. template_clause —— 标准示范条款库
# ============================================================
class TemplateClause(Base):
    """标准示范条款库（SPEC 6.2）"""

    __tablename__ = 'template_clause'

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    contract_type: Mapped[str] = mapped_column(String(32), nullable=False, default='*')
    clause_type: Mapped[str] = mapped_column(String(32), nullable=False, default=ClauseType.OTHER)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default='')
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    rules: Mapped[List["Rule"]] = relationship(
        back_populates='template_clause', foreign_keys=[Rule.template_clause_id]
    )

    __table_args__ = (
        Index('ix_template_type', 'contract_type', 'clause_type'),
    )


# ============================================================
# 7. review_comment —— 回写评论（1:1）
# ============================================================
class ReviewComment(Base):
    """回写评论（SPEC 6.2）"""

    __tablename__ = 'review_comment'

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey('contract_task.id', ondelete='CASCADE'), nullable=False, unique=True
    )

    content_md: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # 格式化 Markdown
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=WritebackStatus.NOT_WRITTEN
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    external_comment_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    task: Mapped["ContractTask"] = relationship(back_populates='comment')


# ============================================================
# 8. task_event_log —— 审计事件日志
# ============================================================
class TaskEventLog(Base):
    """任务事件日志（SPEC 6.2）—— 全流程状态迁移留痕"""

    __tablename__ = 'task_event_log'

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey('contract_task.id', ondelete='CASCADE'), nullable=False
    )

    event: Mapped[str] = mapped_column(String(32), nullable=False)  # CREATE / PARSE_OK / ...
    from_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    to_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    operator: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # 操作人 / system
    detail_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # 上下文快照

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    task: Mapped["ContractTask"] = relationship(back_populates='event_logs')

    __table_args__ = (
        Index('ix_event_task_time', 'task_id', 'created_at'),
    )


# ============================================================
# 9. report —— 导出报告
# ============================================================
class Report(Base):
    """导出报告（SPEC 6.2）"""

    __tablename__ = 'report'

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey('contract_task.id', ondelete='CASCADE'), nullable=False
    )

    format: Mapped[str] = mapped_column(String(8), nullable=False, default=ReportFormat.MD)
    file_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    task: Mapped["ContractTask"] = relationship(back_populates='reports')


# ============================================================
# 10. app_user —— 用户（演示精简版）
# ============================================================
class AppUser(Base):
    """用户（SPEC 6.2）"""

    __tablename__ = 'app_user'

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    password_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)  # bcrypt
    role: Mapped[str] = mapped_column(String(16), nullable=False, default=UserRole.BUSINESS)
    display_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


if __name__ == '__main__':
    # 集成自测：建表 → 关联写入 → 查询 → 约束校验
    import sys
    import tempfile
    from pathlib import Path

    # 用 `python -m db.models` 运行时，本文件被加载为 __main__；
    # 而 db.session.init_db() 内部会 `from db import models`，会把本文件**再执行一遍**，
    # 于是同一批表在同一个 MetaData 上被定义两次 → 报 "Table already defined"。
    # 先把 __main__ 登记成 db.models，让那次 import 直接命中缓存即可。
    sys.modules.setdefault('db.models', sys.modules[__name__])

    from sqlalchemy import inspect, select
    from sqlalchemy.exc import IntegrityError

    from db.session import (
        drop_all, get_engine, init_db, init_engine, reset_engine, session_scope,
    )

    temp_dir = Path(tempfile.mkdtemp(prefix='db_models_test_'))
    reset_engine()
    init_engine(f'sqlite:///{temp_dir.as_posix()}/models_test.db')
    drop_all()
    init_db()

    passed = 0

    # ---------- 一、建表检查 ----------
    expected_tables = {
        'contract_task', 'contract_meta', 'clause', 'risk_item', 'rule',
        'template_clause', 'review_comment', 'task_event_log', 'report', 'app_user',
    }
    actual_tables = set(inspect(get_engine()).get_table_names())
    assert actual_tables == expected_tables, \
        f'表清单不一致\n  缺失: {expected_tables - actual_tables}\n  多出: {actual_tables - expected_tables}'
    passed += 1
    print(f'  ✅ 建表成功，共 {len(actual_tables)} 张表')

    # ---------- 二、默认值 ----------
    from db.constants import ReviewStatus, WritebackStatus

    with session_scope() as session:
        task = ContractTask(contract_name='软件采购合同', external_doc_id='CG-2026-001')
        session.add(task)

    with session_scope() as session:
        loaded = session.scalars(select(ContractTask)).one()
        assert loaded.review_status == ReviewStatus.PENDING, '审查状态默认值应为 pending'
        assert loaded.writeback_status == WritebackStatus.NOT_WRITTEN, '回写状态默认值应为 not_written'
        assert loaded.retry_count == 0, '重试计数默认应为 0'
        assert loaded.created_at is not None and loaded.updated_at is not None, '时间戳应自动填充'
        task_id = loaded.id
    passed += 1
    print('  ✅ 字段默认值（状态/计数/时间戳）正确')

    # ---------- 三、一对多关联写入与查询 ----------
    from db.constants import AdoptStatus, ClauseType, RiskLevel, RiskSource

    with session_scope() as session:
        meta = ContractMeta(
            task_id=task_id, contract_no='CG-2026-001', party_a='某某科技', party_b='某某软件',
            credit_code='91310000MA1FL1234X', amount=Decimal('1280000.00'), currency='CNY',
        )
        clause = Clause(
            task_id=task_id, clause_type=ClauseType.PAYMENT, title='第二条 付款方式',
            content='货到即付全款，未设验收条件。',
            start_offset=100, end_offset=118, page_no=1, paragraph_index=2, seq=0,
        )
        session.add_all([meta, clause])
        session.flush()  # 拿到 clause.id

        session.add(RiskItem(
            task_id=task_id, clause_id=clause.id, dimension='payment', risk_type='timing',
            risk_level=RiskLevel.HIGH, cause='未设置付款前置验收条件',
            legal_basis='《民法典》第626条', source=RiskSource.RULE,
        ))
        session.add(TaskEventLog(
            task_id=task_id, event='CREATE', to_status=ReviewStatus.PENDING, operator='system'
        ))

    with session_scope() as session:
        loaded_task = session.get(ContractTask, task_id)
        assert loaded_task.meta is not None, 'contract_meta 未写入'
        assert loaded_task.meta.credit_code == '91310000MA1FL1234X'
        assert len(loaded_task.clauses) == 1, 'clause 未写入'
        assert len(loaded_task.risk_items) == 1, 'risk_item 未写入'
        assert len(loaded_task.event_logs) == 1, 'task_event_log 未写入'

        risk = loaded_task.risk_items[0]
        assert risk.risk_level == RiskLevel.HIGH
        assert risk.adopt_status == AdoptStatus.PENDING, '采纳状态默认应为 pending'
        assert risk.clause is not None, 'risk_item → clause 关联未建立'
        assert risk.clause.page_no == 1, 'Locator 页码应可穿透查询'
    passed += 1
    print('  ✅ 1对1 / 1对多 / 多对1 关联写入与查询正常')

    # ---------- 四、唯一约束 ----------
    # 4.1 external_doc_id 重复 → 应被拒绝
    try:
        with session_scope() as session:
            session.add(ContractTask(contract_name='重复单据', external_doc_id='CG-2026-001'))
    except IntegrityError:
        passed += 1
        print('  ✅ external_doc_id 唯一约束生效（重复被拒绝）')
    else:
        raise AssertionError('重复的 external_doc_id 未被拒绝')

    # 4.2 多个 NULL 可共存（上传来源不传单据号，不能被唯一索引挡住）
    with session_scope() as session:
        session.add_all([
            ContractTask(contract_name='上传合同A', external_doc_id=None),
            ContractTask(contract_name='上传合同B', external_doc_id=None),
        ])
    with session_scope() as session:
        upload_count = len(session.scalars(
            select(ContractTask).where(ContractTask.external_doc_id.is_(None))
        ).all())
        assert upload_count == 2, f'多个 NULL 应可共存，实际 {upload_count}'
    passed += 1
    print('  ✅ 多个 NULL 的 external_doc_id 可共存（上传来源不被唯一索引挡住）')

    # ---------- 五、外键约束（验证 SQLite PRAGMA 生效）----------
    try:
        with session_scope() as session:
            session.add(Clause(task_id=999999, content='孤儿条款'))
    except IntegrityError:
        passed += 1
        print('  ✅ 外键约束生效（孤儿条款被拒绝）')
    else:
        raise AssertionError('指向不存在任务的条款未被拒绝，外键约束可能没打开')

    # ---------- 六、级联删除 ----------
    with session_scope() as session:
        session.delete(session.get(ContractTask, task_id))

    with session_scope() as session:
        assert session.get(ContractTask, task_id) is None, '任务未删除'
        assert len(session.scalars(select(Clause).where(Clause.task_id == task_id)).all()) == 0, \
            '级联删除未生效：clause 残留'
        assert len(session.scalars(select(RiskItem).where(RiskItem.task_id == task_id)).all()) == 0, \
            '级联删除未生效：risk_item 残留'
        assert len(session.scalars(select(ContractMeta).where(ContractMeta.task_id == task_id)).all()) == 0, \
            '级联删除未生效：contract_meta 残留'
    passed += 1
    print('  ✅ 级联删除生效（任务删除后子表无残留）')

    # ---------- 七、JSON 字段与 to_dict ----------
    with session_scope() as session:
        session.add(Rule(
            code='R-04', name='付款无验收前置', dimension='payment',
            trigger_condition={
                'and': [
                    {'field': 'clause_type', 'op': 'eq', 'value': 'payment'},
                    {'field': 'content', 'op': 'contains_any', 'value': ['到货即付', '货到付款全款']},
                ]
            },
            risk_level=RiskLevel.HIGH, enabled=True, priority=10,
        ))

    with session_scope() as session:
        rule = session.scalars(select(Rule).where(Rule.code == 'R-04')).one()
        assert rule.trigger_condition['and'][0]['field'] == 'clause_type', 'JSON 字段读写异常'
        assert rule.enabled is True, '布尔字段读写异常'
        row = rule.to_dict(exclude=('trigger_condition',))
        assert 'trigger_condition' not in row, 'to_dict 排除字段失败'
        assert row['code'] == 'R-04'
    passed += 1
    print('  ✅ JSON 字段读写与 to_dict 正常')

    reset_engine()
    print(f'db.models 自测全部通过 ✅ 共 {passed} 个用例')
