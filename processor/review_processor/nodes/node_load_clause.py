'''processor/review_processor/nodes/node_load_clause.py'''

'''
步骤分解
1. 接收状态: 取 `task_id`（contract_task.id）。
2. 载入条款: 优先用状态里已有的 `clauses`（内存链路/单测），否则按 task_id 从库里捞。
3. 载入元数据: 同上，从 contract_meta 取出并还原成解析阶段的 meta 结构（含 locators）。
4. 载入合同类型: 从 contract_task.biz_type 取，供规则按合同类型匹配。
5. 输出统计日志。

需求分析
输入:
- `task_id`：**contract_task.id**
- （可选）`clauses` / `meta`：已存在于状态时直接用，避免重复查库

输出:
- `clauses`：Clause[]（含 Locator，且额外带 `db_clause_id` 供后续风险项挂载）
- `meta`：ContractMeta（含 locators）
- `contract_type`：合同类型

为什么审查子图要自己载入数据:
    4 个子图是**相互独立**的（web 层顺序调用），彼此不共享内存状态。
    审查子图只需要 task_id 就能干活，这才叫独立；
    否则 web 层得把 Clause[] 这类大对象在内存里搬来搬去。

关于 `db_clause_id`（重要）:
    解析阶段产出的 `clause_id` 是 `cl_001` 这种展示用编号，
    而 risk_item.clause_id 是数据库外键，必须是 clause 表的自增主键。
    两个 id 都要留着：前者给人看、给前端做联动锚点，后者给库用。

关于条款为空:
    **不报错**。条款为空时规则层仍能跑"文档级"规则（如 R-01 主体缺失），
    报错反而会把这类本该检出的风险挡掉。
'''

import json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from db.models import Clause, ContractMeta, ContractTask
from db.session import session_scope
from processor.review_processor.base import BaseNode, setup_logging
from processor.review_processor.exceptions import (
    ReviewProcessError,
    StateFieldError,
    StorageError,
)
from processor.review_processor.state import ReviewGraphState, get_default_state


class NodeLoadClause(BaseNode):
    '''载入节点：按 task_id 把条款与元数据装进审查图状态'''
    name = 'node_load_clause'

    def process(self, state: ReviewGraphState) -> ReviewGraphState:
        """
        节点：载入条款与元数据（node_load_clause）
        整体流程：取任务ID → 载入条款 → 载入元数据 → 载入合同类型 → 统计日志
        核心目的：让审查子图只依赖 task_id 就能开工，与解析子图完全解耦
        必要参数：task_id
        更新参数：clauses、meta、contract_type

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        # =======步骤一:获取并校验任务ID=======
        db_task_id = self._step_1_get_task_id(state)

        try:
            # =======步骤二:载入任务基础信息（合同类型）=======
            contract_type, task_missing = self._step_2_load_task_info(db_task_id)

            # =======步骤三:载入条款=======
            clauses = self._step_3_load_clauses(state, db_task_id)

            # =======步骤四:载入元数据=======
            meta = self._step_4_load_meta(state, db_task_id)

        except SQLAlchemyError as e:
            raise StorageError(message=f'载入审查数据失败：{e}', cause=e)

        # =======步骤五:写回状态 + 输出统计日志=======
        state['clauses'] = clauses
        state['meta'] = meta
        state['contract_type'] = contract_type

        self._step_5_print_stats(db_task_id, clauses, meta, contract_type, task_missing)

        return state

    def _step_1_get_task_id(self, state: ReviewGraphState) -> int:
        """
        【步骤1】获取并校验任务ID
        :param state: 项目状态字典（ReviewGraphState）
        :return: 整型任务ID
        """
        raw_task_id = state.get('task_id')
        if raw_task_id in (None, ''):
            raise StateFieldError(
                field_name='task_id',
                message='任务ID不能为空',
                expected_type=str
            )
        try:
            return int(raw_task_id)
        except (TypeError, ValueError):
            raise StateFieldError(
                field_name='task_id',
                message=f'task_id 必须是 contract_task.id（数字），当前值为 {raw_task_id!r}',
                expected_type=int
            )

    def _step_2_load_task_info(self, db_task_id: int) -> tuple:
        """
        【步骤2】载入任务基础信息
        功能：取合同类型（biz_type）用于规则匹配；同时判断任务是否存在
        :param db_task_id: 任务ID
        :return: (合同类型, 任务是否缺失)
        """
        with session_scope() as session:
            task = session.get(ContractTask, db_task_id)

        if task is None:
            # 任务不存在不直接报错：先记下来，让调用方在日志里看到
            self.logger.warning(f'任务 {db_task_id} 不存在，将只基于状态中的数据继续')
            return '*', True

        return (task.biz_type or '*'), False

    def _step_3_load_clauses(self, state: ReviewGraphState, db_task_id: int) -> List[Dict[str, Any]]:
        """
        【步骤3】载入条款
        功能：状态里已有则直接用；否则按 task_id 从 clause 表捞出并还原为解析阶段的结构
        :param state: 项目状态字典
        :param db_task_id: 任务ID
        :return: Clause[] 列表
        """
        existing = state.get('clauses')
        if existing:
            self.log_step('条款来源', f'状态（{len(existing)} 条）')
            return existing

        with session_scope() as session:
            rows = session.scalars(
                select(Clause).where(Clause.task_id == db_task_id).order_by(Clause.seq)
            ).all()

            clauses = [
                {
                    # 展示用编号：与解析阶段保持一致，前端联动锚点也用它
                    'clause_id': f'cl_{index + 1:03d}',
                    # 数据库主键：risk_item.clause_id 外键要用它
                    'db_clause_id': row.id,
                    'seq': row.seq,
                    'clause_type': row.clause_type,
                    'title': row.title or '',
                    'content': row.content or '',
                    'start_offset': row.start_offset,
                    'end_offset': row.end_offset,
                    'page_no': row.page_no,
                    'paragraph_index': row.paragraph_index,
                }
                for index, row in enumerate(rows)
            ]

        self.log_step('条款来源', f'数据库（{len(clauses)} 条）')
        return clauses

    def _step_4_load_meta(self, state: ReviewGraphState, db_task_id: int) -> Dict[str, Any]:
        """
        【步骤4】载入合同元数据
        功能：状态里已有则直接用；否则从 contract_meta 取出并还原解析阶段的 meta 结构
        :param state: 项目状态字典
        :param db_task_id: 任务ID
        :return: ContractMeta 字典；没有记录时返回 {}
        """
        existing = state.get('meta')
        if existing:
            return existing

        with session_scope() as session:
            row = session.scalars(
                select(ContractMeta).where(ContractMeta.task_id == db_task_id)
            ).first()

            if row is None:
                return {}

            meta: Dict[str, Any] = {
                'contract_no': row.contract_no or '',
                'party_a': row.party_a or '',
                'party_b': row.party_b or '',
                'credit_code': row.credit_code or '',
                'amount': float(row.amount) if row.amount is not None else None,
                'currency': row.currency or '',
                'period_start': row.period_start.isoformat() if row.period_start else '',
                'period_end': row.period_end.isoformat() if row.period_end else '',
                'effective_condition': row.effective_condition or '',
                'raw_json': row.raw_json,
            }

        # 把 locators 从 raw_json 里还原出来（工作台「元数据高亮」要用）
        meta['locators'] = self._extract_locators(meta.get('raw_json'))
        return meta

    @staticmethod
    def _extract_locators(raw_json: Optional[str]) -> Dict[str, Any]:
        """
        【辅助函数】从 raw_json 还原各字段的 Locator
        :param raw_json: 落库时的原始提取 JSON
        :return: locators 字典；解析失败返回空字典（不因脏数据中断审查）
        """
        if not raw_json:
            return {}
        try:
            return json.loads(raw_json).get('locators', {}) or {}
        except (json.JSONDecodeError, TypeError, AttributeError):
            return {}

    def _step_5_print_stats(self, db_task_id: int, clauses: List[Dict[str, Any]],
                            meta: Dict[str, Any], contract_type: str, task_missing: bool) -> None:
        """
        【步骤5】输出统计日志
        """
        self.log_step('任务ID', str(db_task_id))
        self.log_step('合同类型', contract_type)
        self.log_step('条款数', str(len(clauses)))
        self.log_step('元数据', '已载入' if meta else '无')
        if task_missing:
            self.log_step('提醒', '任务记录不存在，审查结果可能无法回写')

        # 关键字段缺失要显式暴露 —— R-01（主体缺失）就靠它判高风险
        missing_fields = [
            name for name in ('party_a', 'party_b', 'credit_code', 'amount')
            if not meta.get(name)
        ]
        if missing_fields:
            self.log_step('元数据缺失', str(missing_fields))

        if not clauses:
            self.logger.warning(
                '条款为空 —— 文档级规则（如 R-01 主体缺失）仍会执行，'
                '但条款级规则不会产出任何风险项'
            )


if __name__ == '__main__':
    # 激活日志的全局配置
    setup_logging()

    import tempfile
    from pathlib import Path

    from db.models import Clause as ClauseModel
    from db.session import init_db, init_engine, reset_engine, session_scope

    node_load_clause = NodeLoadClause()
    passed = 0

    # 临时库
    work_dir = Path(tempfile.mkdtemp(prefix='node_load_clause_test_'))
    reset_engine()
    init_engine(f'sqlite:///{work_dir.as_posix()}/load_test.db')
    init_db()

    # 准备：一个任务 + 2 条条款 + 元数据
    with session_scope() as session:
        session.add(ContractTask(
            contract_name='软件采购合同', biz_type='采购', source_type='upload',
            review_status='reviewing',
        ))
    with session_scope() as session:
        db_task_id = session.scalars(
            select(ContractTask).where(ContractTask.contract_name == '软件采购合同')
        ).first().id

    with session_scope() as session:
        session.add_all([
            ClauseModel(task_id=db_task_id, clause_type='payment', title='第二条 付款方式',
                        content='货到即付全款。', start_offset=10, end_offset=20,
                        page_no=1, paragraph_index=1, seq=0),
            ClauseModel(task_id=db_task_id, clause_type='ip', title='第三条 知识产权',
                        content='知识产权归乙方所有。', start_offset=21, end_offset=35,
                        page_no=1, paragraph_index=2, seq=1),
        ])
        session.add(ContractMeta(
            task_id=db_task_id, contract_no='CG-2026-001',
            party_a='某某科技有限公司', party_b='某某软件股份有限公司',
            credit_code='91310000MA1FL1234X',
            raw_json=json.dumps({'locators': {'amount': {'start_offset': 1, 'end_offset': 5}}},
                                ensure_ascii=False),
        ))

    # ---------- 一、从数据库载入 ----------
    logging.getLogger().info('=' * 22 + ' 一、从数据库载入 ' + '=' * 22)

    init_state = get_default_state()
    init_state['task_id'] = str(db_task_id)
    result = node_load_clause(init_state)

    assert len(result['clauses']) == 2, f"应载入 2 条条款，实际 {len(result['clauses'])}"
    assert result['clauses'][0]['clause_type'] == 'payment'
    assert result['clauses'][0]['clause_id'] == 'cl_001', '展示用编号应重新生成'
    assert isinstance(result['clauses'][0]['db_clause_id'], int), 'db_clause_id 必须是数据库主键'
    assert result['clauses'][0]['db_clause_id'] > 0, 'db_clause_id 应为有效的自增主键'
    assert result['clauses'][0]['page_no'] == 1, 'Locator 页码应带过来'
    assert result['clauses'][1]['content'] == '知识产权归乙方所有。'

    assert result['contract_type'] == '采购', f"合同类型应载入，实际 {result['contract_type']}"
    assert result['meta']['party_b'] == '某某软件股份有限公司'
    assert result['meta']['credit_code'] == '91310000MA1FL1234X'
    # locators 应能从 raw_json 还原
    assert 'amount' in result['meta']['locators'], 'locators 未从 raw_json 还原'

    passed += 1
    logging.getLogger().info('  ✅ 从数据库正确载入条款/元数据/合同类型（含 locators）')

    # ---------- 二、状态已有数据时不再查库 ----------
    logging.getLogger().info('=' * 22 + ' 二、状态优先 ' + '=' * 22)

    preset_state = get_default_state()
    preset_state['task_id'] = str(db_task_id)
    preset_state['clauses'] = [{'clause_id': 'cl_001', 'content': '内存里的条款'}]
    preset_state['meta'] = {'party_b': '内存里的乙方'}
    result = node_load_clause(preset_state)

    assert result['clauses'] == preset_state['clauses'], '状态已有条款时不应被库数据覆盖'
    assert result['meta']['party_b'] == '内存里的乙方', '状态已有元数据时不应被覆盖'
    passed += 1
    logging.getLogger().info('  ✅ 状态已有数据时优先使用，不覆盖')

    # ---------- 三、任务不存在时不报错 ----------
    logging.getLogger().info('=' * 22 + ' 三、任务不存在 ' + '=' * 22)

    orphan_state = get_default_state()
    orphan_state['task_id'] = '999999'
    result = node_load_clause(orphan_state)
    assert result['clauses'] == [], '任务不存在时条款应为空'
    assert result['contract_type'] == '*', '任务不存在时合同类型应兜底为通配'
    passed += 1
    logging.getLogger().info('  ✅ 任务不存在 → 不报错，合同类型兜底为 *')

    # ---------- 四、无元数据记录 ----------
    logging.getLogger().info('=' * 22 + ' 四、无元数据 ' + '=' * 22)

    with session_scope() as session:
        session.add(ContractTask(contract_name='无元数据合同', source_type='upload'))
    with session_scope() as session:
        bare_id = session.scalars(
            select(ContractTask).where(ContractTask.contract_name == '无元数据合同')
        ).first().id

    bare_state = get_default_state()
    bare_state['task_id'] = str(bare_id)
    result = node_load_clause(bare_state)
    assert result['meta'] == {}, '无 contract_meta 记录时应返回空字典'
    assert result['contract_type'] == '*', 'biz_type 为空时应兜底为通配'
    passed += 1
    logging.getLogger().info('  ✅ 无元数据 → 返回空字典，不报错')

    # ---------- 五、异常场景 ----------
    logging.getLogger().info('=' * 22 + ' 五、异常场景 ' + '=' * 22)

    err_cases = [
        ('任务ID为空', '', StateFieldError),
        ('任务ID非数字', 'uuid-abc', StateFieldError),
    ]
    for case_name, bad_task_id, expected_exc in err_cases:
        bad_state = get_default_state()
        bad_state['task_id'] = bad_task_id
        try:
            node_load_clause(bad_state)
        except Exception as e:
            assert isinstance(e, ReviewProcessError), f'{case_name} 应被基类包装成 ReviewProcessError'
            assert isinstance(e.cause, expected_exc), \
                f'{case_name} 应抛出 {expected_exc.__name__}，实际 {type(e.cause).__name__}'
            passed += 1
            logging.getLogger().info(f'  ✅ {case_name:<10} → 正确抛出 {expected_exc.__name__}')
        else:
            raise AssertionError(f'{case_name} 未抛出异常，用例失败')

    reset_engine()
    logging.getLogger().info(f'node_load_clause 自测全部通过 ✅ 共 {passed} 个用例')
