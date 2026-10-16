'''processor/recommend_processor/nodes/node_load_risk.py'''

'''
步骤分解
1. 接收状态: 取 `task_id`（contract_task.id）。
2. 载入风险项: 优先用状态里已有的 `risk_items`（内存链路/单测），否则按 task_id 从库里捞；
             并补齐 `db_risk_id`（回填 suggestion_text 的主键）、规则绑定模板、法务处理状态。
3. 按等级过滤: 只保留不低于 `RECOMMEND_MIN_RISK_LEVEL` 的风险点（默认 low = 全部）。
4. 载入条款: 按 task_id 取 clause 表，建 `clause_map`，并把条款上下文挂到每条风险项上。
5. 载入元数据与合同类型。
6. 输出统计日志。

需求分析
输入:
- `task_id`：**contract_task.id**
- （可选）`risk_items`：已存在于状态时直接用

输出:
- `risk_items`：待生成建议的风险项（每条都带条款原文、条款类型、规则绑定模板、法务处理状态）
- `clause_map`：{str(db_clause_id): 条款字典}，供改写与差异对比取原文
- `meta` / `contract_type`

为什么只依赖 task_id 就能开工:
    与 review_processor 的设计一致 —— 4 个子图相互独立，web 层顺序调用，
    彼此通过数据库交接而不是内存里搬大对象。

为什么文档级风险也要处理:
    R-01「签约主体缺失」这类风险 `clause_id` 是 NULL，没有对应条款原文。
    但它恰恰是最需要给出建议的一类（要补一条主体资质条款）。
    所以原文为空时不能跳过，而是走「无原文 → 只生成新增建议」的路径。

为什么要过滤等级:
    低风险里常有大量「措辞不够严谨」类的提示，逐条调 LLM 生成改写既费钱又淹没重点。
    用 RECOMMEND_MIN_RISK_LEVEL 可把生成范围收紧到 mid/high，默认仍为 low（全部生成）。
'''

import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from db.constants import AdoptStatus, ClauseType, RiskLevel
from db.models import Clause, ContractMeta, ContractTask, RiskItem, Rule
from db.session import session_scope
from processor.recommend_processor.base import BaseNode, setup_logging
from processor.recommend_processor.exceptions import (
    RecommendProcessError,
    StateFieldError,
    StorageError,
)
from processor.recommend_processor.state import RecommendGraphState, get_default_state


class NodeLoadRisk(BaseNode):
    '''载入节点：按 task_id 把待生成建议的风险项与条款上下文装进推荐图状态'''
    name = 'node_load_risk'

    def process(self, state: RecommendGraphState) -> RecommendGraphState:
        """
        节点：载入风险项与条款上下文（node_load_risk）
        整体流程：取任务ID → 载入风险项 → 等级过滤 → 载入条款 → 载入元数据 → 统计日志
        核心目的：让推荐子图只依赖 task_id 就能开工，与审查子图完全解耦
        必要参数：task_id
        更新参数：risk_items、clause_map、meta、contract_type

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        # =======步骤一:获取并校验任务ID=======
        db_task_id = self._step_1_get_task_id(state)

        try:
            # =======步骤二:载入风险项=======
            risk_items = self._step_2_load_risk_items(state, db_task_id)

            # =======步骤三:按等级过滤=======
            risk_items = self._step_3_filter_by_level(risk_items)

            # =======步骤四:载入条款原文=======
            clause_map = self._step_4_load_clauses(db_task_id)

            # =======步骤五:载入元数据与合同类型=======
            meta, contract_type = self._step_5_load_task_context(db_task_id)

            # =======步骤六:为风险项挂上条款上下文=======
            self._step_6_attach_clause_context(risk_items, clause_map)

        except SQLAlchemyError as e:
            raise StorageError(message=f'载入推荐数据失败：{e}', cause=e)

        # =======步骤七:写回状态 + 输出统计日志=======
        state['risk_items'] = risk_items
        state['clause_map'] = clause_map
        state['meta'] = meta
        state['contract_type'] = contract_type

        self._step_7_print_stats(db_task_id, risk_items, clause_map, meta, contract_type)

        return state

    def _step_1_get_task_id(self, state: RecommendGraphState) -> int:
        """
        【步骤1】获取并校验任务ID
        :param state: 项目状态字典（RecommendGraphState）
        :return: 整型任务ID
        """
        raw_task_id = state.get('task_id')
        if raw_task_id in (None, ''):
            raise StateFieldError(field_name='task_id', message='任务ID不能为空', expected_type=str)

        try:
            return int(raw_task_id)
        except (TypeError, ValueError):
            raise StateFieldError(
                field_name='task_id',
                message=f'task_id 必须是 contract_task.id（数字），当前值为 {raw_task_id!r}',
                expected_type=int
            )

    def _step_2_load_risk_items(self, state: RecommendGraphState,
                                db_task_id: int) -> List[Dict[str, Any]]:
        """
        【步骤2】载入风险项
        功能：状态里已有则用状态，否则查库；两种情况都补齐 db_risk_id、
             规则绑定模板、法务处理状态（回填建议时要用）
        :param state: 项目状态字典
        :param db_task_id: 任务ID
        :return: 风险项列表
        """
        existing = state.get('risk_items')
        if existing:
            self.log_step('风险项来源', f'状态（{len(existing)} 条）')
            return self._enrich_items(existing, db_task_id)

        with session_scope() as session:
            rows = session.scalars(
                select(RiskItem).where(RiskItem.task_id == db_task_id).order_by(RiskItem.id)
            ).all()

            rule_map = self._load_rule_map(session, [row.matched_rule_id for row in rows])

            items = [
                {
                    'db_risk_id': row.id,
                    'rule_code': rule_map.get(row.matched_rule_id, {}).get('code', ''),
                    'matched_rule_id': row.matched_rule_id,
                    # 规则直接绑定的模板 → 召回时最优先（SPEC 4.3.3 的规则库都带了 template_clause_id）
                    'template_clause_id': rule_map.get(row.matched_rule_id, {}).get('template_clause_id'),
                    # 展示用编号留空，由步骤六从 clause_map 统一回填（避免两处各算一遍）
                    'clause_id': '',
                    'db_clause_id': row.clause_id,
                    'page_no': 0,
                    'paragraph_index': 0,
                    'dimension': row.dimension or '',
                    'risk_type': row.risk_type or '',
                    'risk_level': row.risk_level or RiskLevel.LOW,
                    'cause': row.cause or '',
                    'legal_basis': row.legal_basis or '',
                    'evidence': '',
                    'suggestion': row.suggestion_text or '',
                    'source': row.source or '',
                    'adopt_status': row.adopt_status or AdoptStatus.PENDING,
                    'manual_comment': row.manual_comment or '',
                }
                for row in rows
            ]

        self.log_step('风险项来源', f'数据库（{len(items)} 条）')
        return items

    @staticmethod
    def _load_rule_map(session, rule_ids: List[Optional[int]]) -> Dict[int, Dict[str, Any]]:
        """
        【辅助函数】批量载入规则信息
        功能：一次查完所有用到的规则，避免逐条查询
        :param session: 数据库会话
        :param rule_ids: 规则主键列表（可含 None）
        :return: {rule_id: {'code':.., 'template_clause_id':..}}
        """
        valid_ids = {rid for rid in rule_ids if rid is not None}
        if not valid_ids:
            return {}

        rows = session.scalars(select(Rule).where(Rule.id.in_(valid_ids))).all()
        return {
            row.id: {'code': row.code, 'template_clause_id': row.template_clause_id}
            for row in rows
        }

    def _enrich_items(self, items: List[Dict[str, Any]],
                      db_task_id: int) -> List[Dict[str, Any]]:
        """
        【辅助函数】为状态里传入的风险项补齐库中的可用信息
        功能：状态里的风险项（多来自审查子图的内存状态）没有 db_risk_id 与法务处理状态，
             这里按业务键 (条款, 维度, 风险类型) 回查库补上 ——
             否则落库节点无法定位记录，只能整批跳过。
        :param items: 状态传入的风险项
        :param db_task_id: 任务ID
        :return: 补齐后的风险项列表
        """
        with session_scope() as session:
            rows = session.scalars(
                select(RiskItem).where(RiskItem.task_id == db_task_id)
            ).all()
            index = {
                self._business_key(row.clause_id, row.dimension, row.risk_type): row
                for row in rows
            }
            rule_map = self._load_rule_map(session, [row.matched_rule_id for row in rows])

            enriched: List[Dict[str, Any]] = []
            for item in items:
                new_item = dict(item)
                row = index.get(self._business_key(
                    item.get('db_clause_id'), item.get('dimension'), item.get('risk_type')
                ))

                if row is not None:
                    new_item.setdefault('db_risk_id', row.id)
                    new_item['adopt_status'] = row.adopt_status or AdoptStatus.PENDING
                    new_item['manual_comment'] = row.manual_comment or ''
                    new_item.setdefault('legal_basis', row.legal_basis or '')
                    new_item.setdefault('suggestion', row.suggestion_text or '')
                    rule_info = rule_map.get(row.matched_rule_id, {})
                    new_item.setdefault('template_clause_id', rule_info.get('template_clause_id'))
                    new_item.setdefault('rule_code', rule_info.get('code', ''))
                    new_item.setdefault('page_no', 0)
                    new_item.setdefault('paragraph_index', 0)
                else:
                    # 库里没有对应记录 → 保持 None，落库节点会跳过并告警
                    new_item.setdefault('db_risk_id', None)

                new_item.setdefault('adopt_status', AdoptStatus.PENDING)
                new_item.setdefault('manual_comment', '')
                enriched.append(new_item)

        return enriched

    @staticmethod
    def _business_key(clause_id: Optional[int], dimension: Optional[str],
                      risk_type: Optional[str]) -> Tuple:
        """【辅助函数】风险项业务键（与 node_persist_risk 保持一致，不含风险等级）"""
        return (clause_id or 0, dimension or '', risk_type or '')

    def _step_3_filter_by_level(self, risk_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        【步骤3】按等级过滤
        功能：只保留不低于 RECOMMEND_MIN_RISK_LEVEL 的风险点；并做严重度降序排序，
             让高风险的建议排在最前（法务从上往下处理即可）
        :param risk_items: 全部风险项
        :return: 过滤并排序后的风险项
        """
        kept = [
            item for item in risk_items
            if self.config.is_risk_in_scope(item.get('risk_level') or '')
        ]
        dropped = len(risk_items) - len(kept)
        if dropped:
            self.log_step(
                '等级过滤',
                f"低于 {self.config.min_risk_level} 的风险点 {dropped} 条不生成建议"
            )

        kept.sort(
            key=lambda item: (
                -RiskLevel.SEVERITY.get(item.get('risk_level') or '', 0),
                item.get('db_risk_id') or 0,
            )
        )
        return kept

    def _step_4_load_clauses(self, db_task_id: int) -> Dict[str, Any]:
        """
        【步骤4】载入条款原文
        功能：按 task_id 取全部条款，建 {str(id): 条款字典} 索引
        :param db_task_id: 任务ID
        :return: clause_map
        """
        with session_scope() as session:
            rows = session.scalars(
                select(Clause).where(Clause.task_id == db_task_id).order_by(Clause.seq)
            ).all()

            clause_map = {
                str(row.id): {
                    'db_clause_id': row.id,
                    'clause_id': f'cl_{row.seq + 1:03d}',
                    'seq': row.seq,
                    'clause_type': row.clause_type,
                    'title': row.title or '',
                    'content': row.content or '',
                    'page_no': row.page_no,
                    'paragraph_index': row.paragraph_index,
                }
                for row in rows
            }

        return clause_map

    def _step_5_load_task_context(self, db_task_id: int) -> Tuple[Dict[str, Any], str]:
        """
        【步骤5】载入元数据与合同类型
        :param db_task_id: 任务ID
        :return: (元数据字典, 合同类型)
        """
        with session_scope() as session:
            task = session.get(ContractTask, db_task_id)
            meta_row = session.scalars(
                select(ContractMeta).where(ContractMeta.task_id == db_task_id)
            ).first()

            contract_type = (task.biz_type or '*') if task is not None else '*'

            meta: Dict[str, Any] = {}
            if meta_row is not None:
                meta = {
                    'contract_no': meta_row.contract_no or '',
                    'party_a': meta_row.party_a or '',
                    'party_b': meta_row.party_b or '',
                    'credit_code': meta_row.credit_code or '',
                    'amount': float(meta_row.amount) if meta_row.amount is not None else None,
                    'currency': meta_row.currency or '',
                    'effective_condition': meta_row.effective_condition or '',
                }

        return meta, contract_type

    def _step_6_attach_clause_context(self, risk_items: List[Dict[str, Any]],
                                      clause_map: Dict[str, Any]) -> None:
        """
        【步骤6】为风险项挂上条款上下文（就地修改）
        功能：把条款原文、标题、类型挂到风险项上，供模板召回与差异对比直接使用；
             并处理"条款已不存在"与"文档级风险"两种情况
        :param risk_items: 风险项列表（就地修改）
        :param clause_map: 条款索引
        """
        orphan_count = 0
        document_level_count = 0

        for item in risk_items:
            db_clause_id = item.get('db_clause_id')
            clause = clause_map.get(str(db_clause_id)) if db_clause_id is not None else None

            if clause is not None:
                item['clause_type'] = clause['clause_type']
                item['clause_title'] = clause['title']
                item['clause_content'] = clause['content']
                item['clause_id'] = clause['clause_id']
                item['page_no'] = item.get('page_no') or clause['page_no']
                item['paragraph_index'] = item.get('paragraph_index') or clause['paragraph_index']
                continue

            if db_clause_id is None:
                document_level_count += 1
            else:
                # 条款被删了（如重跑解析）→ 按风险维度兜底推类型，不让召回彻底失效
                orphan_count += 1
                self.logger.warning(
                    f'风险项 {item.get("db_risk_id")} 指向的条款 {db_clause_id} 已不存在，'
                    f'按风险维度推条款类型'
                )

            item['clause_title'] = ''
            item['clause_content'] = ''
            item['clause_type'] = self._guess_clause_type(item)

        if document_level_count:
            self.log_step('文档级风险', f'{document_level_count} 条（无对应条款，将生成新增条款建议）')
        if orphan_count:
            self.log_step('条款缺失', f'{orphan_count} 条（按风险维度兜底推类型）')

    @staticmethod
    def _guess_clause_type(item: Dict[str, Any]) -> str:
        """
        【辅助函数】由风险维度推断条款类型
        说明：SPEC 6.2 的 RiskDimension 取值与 ClauseType 基本同名
             （subject / payment / breach / ip / data_security / confidentiality /
               jurisdiction / acceptance），可直接复用；
             推不出来则归 other，让召回退化为"该类型无候选"。
        :param item: 风险项
        :return: 条款类型
        """
        dimension = (item.get('dimension') or '').strip()
        return dimension if dimension in ClauseType.ALL else ClauseType.OTHER

    def _step_7_print_stats(self, db_task_id: int, risk_items: List[Dict[str, Any]],
                            clause_map: Dict[str, Any], meta: Dict[str, Any],
                            contract_type: str) -> None:
        """
        【步骤7】输出统计日志
        """
        level_counter: Dict[str, int] = {}
        for item in risk_items:
            level = item.get('risk_level') or 'unknown'
            level_counter[level] = level_counter.get(level, 0) + 1

        self.log_step('任务ID', str(db_task_id))
        self.log_step('合同类型', contract_type)
        self.log_step('待生成建议', f'{len(risk_items)} 条')
        self.log_step('等级分布', str(level_counter))
        self.log_step('条款数', str(len(clause_map)))
        self.log_step('元数据', '已载入' if meta else '无')

        missing_fields = [
            name for name in ('party_a', 'party_b', 'amount') if not meta.get(name)
        ]
        if missing_fields:
            self.log_step('元数据缺失', str(missing_fields))

        if not risk_items:
            self.logger.info('无待生成建议的风险点 —— 推荐子图将直接结束')


if __name__ == '__main__':
    # 激活日志的全局配置
    setup_logging()

    import tempfile
    from pathlib import Path

    from db.constants import RiskSource
    from db.session import init_db, init_engine, reset_engine
    from processor.recommend_processor.config import RecommendConfig

    node_load_risk = NodeLoadRisk()
    passed = 0

    # 临时库
    work_dir = Path(tempfile.mkdtemp(prefix='node_load_risk_test_'))
    reset_engine()
    init_engine(f'sqlite:///{work_dir.as_posix()}/load_risk_test.db')
    init_db()

    # 准备：规则 + 模板 + 任务 + 2 条条款 + 元数据 + 3 条风险项（含文档级）
    with session_scope() as session:
        from db.models import TemplateClause
        session.add(TemplateClause(contract_type='*', clause_type='payment',
                                   title='付款与验收前置条款（示范）', content='验收后付款。'))
    with session_scope() as session:
        template_id = session.scalars(select(TemplateClause)).first().id
        session.add(Rule(code='R-04', name='付款无验收前置', dimension='payment',
                         risk_level='high', template_clause_id=template_id))
    with session_scope() as session:
        rule_id = session.scalars(select(Rule)).first().id
        session.add(ContractTask(contract_name='推荐测试合同', biz_type='采购',
                                 source_type='upload', review_status='completed'))
    with session_scope() as session:
        db_task_id = session.scalars(select(ContractTask)).first().id
    with session_scope() as session:
        session.add_all([
            Clause(task_id=db_task_id, clause_type='payment', title='第二条 付款方式',
                   content='货到即付全款，未设验收条件。', page_no=1, paragraph_index=1, seq=0),
            Clause(task_id=db_task_id, clause_type='ip', title='第三条 知识产权',
                   content='知识产权归乙方所有。', page_no=1, paragraph_index=2, seq=1),
        ])
        session.add(ContractMeta(task_id=db_task_id, party_a='某某科技有限公司',
                                 party_b='某某软件股份有限公司',
                                 amount=1280000, currency='CNY'))
    with session_scope() as session:
        clause_rows = session.scalars(select(Clause).order_by(Clause.seq)).all()
        payment_clause_id, ip_clause_id = clause_rows[0].id, clause_rows[1].id
    with session_scope() as session:
        session.add_all([
            RiskItem(task_id=db_task_id, clause_id=payment_clause_id, dimension='payment',
                     risk_type='timing', risk_level='high', cause='付款无验收前置',
                     legal_basis='《民法典》第626条', suggestion_text='原建议',
                     matched_rule_id=rule_id, source=RiskSource.RULE),
            RiskItem(task_id=db_task_id, clause_id=ip_clause_id, dimension='ip',
                     risk_type='unbalanced', risk_level='mid', cause='IP 归乙方',
                     source=RiskSource.LLM),
            RiskItem(task_id=db_task_id, clause_id=None, dimension='subject',
                     risk_type='missing', risk_level='high', cause='相对方主体缺失',
                     source=RiskSource.RULE),
            # 低风险项：用于验证 RECOMMEND_MIN_RISK_LEVEL 的过滤行为
            RiskItem(task_id=db_task_id, clause_id=payment_clause_id, dimension='acceptance',
                     risk_type='missing', risk_level='low', cause='验收标准表述不够量化',
                     source=RiskSource.LLM),
        ])

    # ---------- 一、从数据库载入 ----------
    logging.getLogger().info('=' * 22 + ' 一、从数据库载入 ' + '=' * 22)

    state = get_default_state()
    state['task_id'] = str(db_task_id)
    result = node_load_risk(state)

    assert len(result['risk_items']) == 4, f"应载入 4 条风险项，实际 {len(result['risk_items'])}"
    # 严重度降序：high 在前、low 在最后
    assert result['risk_items'][0]['risk_level'] == 'high'
    assert result['risk_items'][-1]['risk_level'] == 'low'

    payment_item = next(i for i in result['risk_items'] if i['dimension'] == 'payment')
    assert payment_item['db_risk_id'] is not None, '必须带数据库主键才能回填'
    assert payment_item['clause_content'] == '货到即付全款，未设验收条件。', '必须挂上条款原文'
    assert payment_item['clause_type'] == 'payment', '条款类型用于模板召回'
    assert payment_item['clause_title'] == '第二条 付款方式'
    assert payment_item['rule_code'] == 'R-04', '规则编码应带过来'
    assert payment_item['template_clause_id'] is not None, '规则绑定的模板必须带过来'
    assert payment_item['adopt_status'] == 'pending'
    assert payment_item['page_no'] == 1

    assert result['contract_type'] == '采购'
    assert result['meta']['party_a'] == '某某科技有限公司'
    assert len(result['clause_map']) == 2, 'clause_map 应含 2 条条款'
    passed += 1
    logging.getLogger().info('  ✅ 风险项/条款上下文/规则绑定模板/元数据全部载入')

    # ---------- 二、文档级风险（clause_id 为 NULL）----------
    logging.getLogger().info('=' * 22 + ' 二、文档级风险 ' + '=' * 22)

    doc_item = next(i for i in result['risk_items'] if i['db_clause_id'] is None)
    assert doc_item['clause_content'] == '', '文档级风险没有条款原文'
    assert doc_item['clause_type'] == 'subject', '应按风险维度兜底推条款类型'
    assert doc_item['clause_id'] == '', '文档级风险没有展示用条款编号'
    passed += 1
    logging.getLogger().info('  ✅ 文档级风险 → 无原文但类型兜底为 subject，不会被跳过')

    # ---------- 三、状态优先 + 按业务键补齐 db_risk_id ----------
    logging.getLogger().info('=' * 22 + ' 三、状态优先并补齐主键 ' + '=' * 22)

    preset_state = get_default_state()
    preset_state['task_id'] = str(db_task_id)
    preset_state['risk_items'] = [{
        'clause_id': 'cl_001', 'db_clause_id': payment_clause_id,
        'dimension': 'payment', 'risk_type': 'timing', 'risk_level': 'high',
        'cause': '内存里的风险', 'suggestion': '内存里的建议',
    }]
    result = node_load_risk(preset_state)

    assert len(result['risk_items']) == 1, '状态已有风险项时不应被库数据覆盖'
    assert result['risk_items'][0]['db_risk_id'] is not None, \
        '状态传入的风险项应按业务键回查补齐 db_risk_id'
    assert result['risk_items'][0]['cause'] == '内存里的风险', '不应覆盖状态里的内容'
    passed += 1
    logging.getLogger().info('  ✅ 状态优先，且按业务键回查补齐 db_risk_id')

    # ---------- 四、等级过滤 ----------
    logging.getLogger().info('=' * 22 + ' 四、等级过滤 ' + '=' * 22)

    strict_state = get_default_state()
    strict_state['task_id'] = str(db_task_id)
    result = NodeLoadRisk(config=RecommendConfig(min_risk_level='mid'))(strict_state)
    assert len(result['risk_items']) == 3, \
        f'min_risk_level=mid 应过滤掉低风险，实际剩 {len(result["risk_items"])} 条'
    assert all(i['risk_level'] != 'low' for i in result['risk_items'])
    passed += 1
    logging.getLogger().info('  ✅ min_risk_level=mid → 低风险不生成建议')

    # ---------- 五、无风险项 ----------
    logging.getLogger().info('=' * 22 + ' 五、无风险项 ' + '=' * 22)

    with session_scope() as session:
        session.add(ContractTask(contract_name='无风险合同', source_type='upload'))
    with session_scope() as session:
        empty_task_id = session.scalars(
            select(ContractTask).where(ContractTask.contract_name == '无风险合同')
        ).first().id

    empty_state = get_default_state()
    empty_state['task_id'] = str(empty_task_id)
    result = node_load_risk(empty_state)
    assert result['risk_items'] == [], '无风险项时应返回空列表'
    assert result['contract_type'] == '*', 'biz_type 为空应兜底为通配'
    passed += 1
    logging.getLogger().info('  ✅ 无风险项 → 空列表，不报错')

    # ---------- 六、异常场景 ----------
    logging.getLogger().info('=' * 22 + ' 六、异常场景 ' + '=' * 22)

    for case_name, bad_task_id in (('任务ID为空', ''), ('任务ID非数字', 'uuid-abc')):
        bad_state = get_default_state()
        bad_state['task_id'] = bad_task_id
        try:
            node_load_risk(bad_state)
        except Exception as e:
            assert isinstance(e, RecommendProcessError), f'{case_name} 应被包装成 RecommendProcessError'
            assert isinstance(e.cause, StateFieldError), \
                f'{case_name} 应抛 StateFieldError，实际 {type(e.cause).__name__}'
            passed += 1
            logging.getLogger().info(f'  ✅ {case_name:<10} → 正确抛出 StateFieldError')
        else:
            raise AssertionError(f'{case_name} 未抛出异常，用例失败')

    reset_engine()
    logging.getLogger().info(f'node_load_risk 自测全部通过 ✅ 共 {passed} 个用例')
