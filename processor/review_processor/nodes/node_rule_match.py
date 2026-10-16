'''processor/review_processor/nodes/node_rule_match.py'''

'''
步骤分解
1. 接收状态: 取 `clauses` / `meta` / `contract_type`。
2. 载入规则: 从 rule 表取启用规则（带内存缓存与 TTL 热加载，SPEC 9.1）。
3. 分派求值: 按规则的 `scope` 决定求值方式
     - scope = clause    → 逐条条款求值，命中即产出一条风险项
     - scope = document  → 整份合同求值一次（用于 R-01 这类看元数据/跨条款的规则）
4. 组装风险项: 把命中的规则翻译成 RiskItem（含 dimension / risk_type / level / 依据 / 证据）。
5. 写回状态 + 输出统计日志。

需求分析
输入:
- `clauses`：Clause[]（来自 node_load_clause）
- `meta`：ContractMeta
- `contract_type`：合同类型，用于规则匹配

输出:
- `rules`：本次载入的启用规则
- `rule_risk_items`：规则层产出的风险项列表

条件树求值（SPEC 9.2）:
    逻辑节点: {"and": [...]} / {"or": [...]} / {"not": {...}}
    叶子节点: {"field": "...", "op": "...", "value": ...}
    支持算子: eq / contains / contains_any / regex / not_contains / not_regex /
             exists / missing / gt / lt
    （其中 not_regex 是对 SPEC 算子集的补充，原因见 db/seed.py 的说明）

健壮性约定（重要）:
    单条规则的条件非法（未知算子、非法正则、字段名写错）**不能让整次审查挂掉** ——
    只跳过该规则并告警。规则是管理员在页面上配的，配置错误很常见，
    不能因为一条坏规则就把整份合同的审查结果全丢掉。
'''

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from db.constants import RiskSource
from db.models import Rule
from db.session import session_scope
from processor.review_processor.base import BaseNode, setup_logging
from processor.review_processor.exceptions import (
    ReviewProcessError,
    RuleEngineError,
    StorageError,
)
from processor.review_processor.state import ReviewGraphState, get_default_state

# 证据片段在 cause 里的最大长度（cause 列限 512 字符）
EVIDENCE_IN_CAUSE_MAX = 120

# ==================== 规则内存缓存（SPEC 9.1 RuleRegistry）====================
_rule_cache: List[Dict[str, Any]] = []
_rule_cache_at: float = 0.0


def _rule_to_dict(rule: Rule) -> Dict[str, Any]:
    """
    【辅助函数】ORM 规则对象转字典（只保留求值需要的字段）
    :param rule: Rule 实例
    :return: 规则字典
    """
    return {
        'id': rule.id,
        'code': rule.code,
        'name': rule.name,
        'contract_type': rule.contract_type,
        'dimension': rule.dimension,
        'risk_level': rule.risk_level,
        'legal_basis': rule.legal_basis,
        'template_clause_id': rule.template_clause_id,
        'priority': rule.priority,
        'trigger_condition': rule.trigger_condition or {},
    }


def load_enabled_rules(force_refresh: bool = False,
                       ttl_seconds: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    载入启用的审查规则（带缓存）
    :param force_refresh: 强制刷新缓存
    :param ttl_seconds: 缓存有效期（秒），None 表示用默认 60
    :return: 规则字典列表，按 priority 升序
    """
    global _rule_cache, _rule_cache_at

    ttl = 60 if ttl_seconds is None else ttl_seconds
    now = time.monotonic()

    if not force_refresh and _rule_cache and (now - _rule_cache_at) < ttl:
        return _rule_cache

    try:
        with session_scope() as session:
            rows = session.scalars(
                select(Rule).where(Rule.enabled.is_(True)).order_by(Rule.priority, Rule.id)
            ).all()
            _rule_cache = [_rule_to_dict(row) for row in rows]
    except SQLAlchemyError as e:
        raise StorageError(message=f'载入审查规则失败：{e}', cause=e)

    _rule_cache_at = now
    return _rule_cache


def reset_rule_cache() -> None:
    """清空规则缓存（测试或管理员改规则后调用）"""
    global _rule_cache, _rule_cache_at
    _rule_cache = []
    _rule_cache_at = 0.0


class NodeRuleMatch(BaseNode):
    '''规则引擎节点：用确定性硬规则快速筛查风险（零成本、低延迟）'''
    name = 'node_rule_match'

    # 支持的算子集合
    _OPERATORS = (
        'eq', 'contains', 'contains_any', 'regex', 'not_contains', 'not_regex',
        'exists', 'missing', 'gt', 'lt',
    )
    # 逻辑节点关键字
    _LOGIC_KEYS = ('and', 'or', 'not')

    def process(self, state: ReviewGraphState) -> ReviewGraphState:
        """
        节点：规则引擎筛查（node_rule_match）
        整体流程：取输入 → 载入规则 → 分派求值 → 组装风险项 → 写回状态
        核心目的：用确定性规则先筛一遍，这部分结果**永不因 LLM 失败而丢失**
                 （SPEC 8.5：规则结果始终保留并优先展示）
        必要参数：无（clauses 为空也允许，文档级规则仍要执行）
        更新参数：rules、rule_risk_items

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        # =======步骤一:获取输入=======
        clauses, meta, contract_type = self._step_1_get_inputs(state)

        # =======步骤二:载入启用规则=======
        rules = load_enabled_rules(ttl_seconds=self.config.rule_cache_ttl_seconds)

        # =======步骤三:逐条规则求值，组装风险项=======
        started_at = time.perf_counter()
        risk_items, skipped_rules, matched_rule_count = self._step_3_evaluate_rules(
            rules, clauses, meta, contract_type
        )
        elapsed_ms = (time.perf_counter() - started_at) * 1000

        # =======步骤四:写回状态 + 输出统计日志=======
        state['rules'] = rules
        state['rule_risk_items'] = risk_items
        self._step_4_print_stats(rules, risk_items, skipped_rules, matched_rule_count, elapsed_ms)

        return state

    def _step_1_get_inputs(self, state: ReviewGraphState) -> Tuple[List[Dict[str, Any]],
                                                                   Dict[str, Any], str]:
        """
        【步骤1】获取输入
        说明：clauses 允许为空 —— 文档级规则（如 R-01）不依赖条款
        :param state: 项目状态字典（ReviewGraphState）
        :return: (条款列表, 元数据, 合同类型)
        """
        clauses = state.get('clauses') or []
        meta = state.get('meta') or {}
        contract_type = state.get('contract_type') or '*'

        if not clauses:
            self.logger.warning('条款为空，将只执行文档级规则')

        return clauses, meta, contract_type

    def _step_3_evaluate_rules(self, rules: List[Dict[str, Any]], clauses: List[Dict[str, Any]],
                               meta: Dict[str, Any], contract_type: str) -> Tuple[
            List[Dict[str, Any]], List[str], int]:
        """
        【步骤3】逐条规则求值
        功能：按 scope 分派；单条规则出错只跳过该规则，不影响其他规则
        :param rules: 启用规则列表
        :param clauses: 条款列表
        :param meta: 元数据
        :param contract_type: 合同类型
        :return: (风险项列表, 被跳过的规则描述, 命中的规则数)
        """
        risk_items: List[Dict[str, Any]] = []
        skipped_rules: List[str] = []
        matched_codes = set()

        # 文档级上下文：把整份合同的派生信息也放进去，便于规则引用
        document_context = self._build_document_context(clauses, meta)

        for rule in rules:
            # 1.合同类型匹配：'*' 为通用规则
            rule_contract_type = rule.get('contract_type') or '*'
            if rule_contract_type != '*' and rule_contract_type != contract_type:
                self.logger.debug(
                    f'规则 {rule["code"]} 仅适用于 {rule_contract_type}，当前 {contract_type}，跳过'
                )
                continue

            # 2.解析条件信封
            try:
                scope, risk_type, condition = self._parse_envelope(rule)
            except RuleEngineError as e:
                skipped_rules.append(f'{rule["code"]}({e})')
                self.logger.warning(f'规则 {rule["code"]} 条件格式非法，已跳过：{e}')
                continue

            # 3.按 scope 分派求值
            try:
                if scope == 'document':
                    hit = self._evaluate_condition(condition, {'clause': {}, 'meta': meta,
                                                               'document': document_context})
                    if hit:
                        risk_items.append(self._build_risk_item(
                            rule, risk_type, clause=None, meta=meta, scope=scope
                        ))
                        matched_codes.add(rule['code'])
                else:
                    for clause in clauses:
                        context = {
                            'clause': clause,
                            'meta': meta,
                            'document': document_context,
                        }
                        if self._evaluate_condition(condition, context):
                            risk_items.append(self._build_risk_item(
                                rule, risk_type, clause=clause, meta=meta, scope=scope
                            ))
                            matched_codes.add(rule['code'])
            except RuleEngineError as e:
                skipped_rules.append(f'{rule["code"]}({e})')
                self.logger.warning(f'规则 {rule["code"]} 求值失败，已跳过：{e}')

        return risk_items, skipped_rules, len(matched_codes)

    def _parse_envelope(self, rule: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
        """
        【辅助函数】解析规则的条件信封
        功能：支持两种写法 ——
             1. 信封形式 {"scope": ..., "risk_type": ..., "when": {...}}
             2. SPEC 9.2 简写形式（根节点直接是 and/or/not，整棵树即条件树）
        :param rule: 规则字典
        :return: (scope, risk_type, 条件树)
        """
        raw = rule.get('trigger_condition') or {}
        if not isinstance(raw, dict) or not raw:
            raise RuleEngineError(message='trigger_condition 为空或不是字典')

        # 简写形式：根节点即逻辑节点
        if any(key in raw for key in self._LOGIC_KEYS):
            return 'clause', 'high_risk_exp', raw

        condition = raw.get('when')
        if not isinstance(condition, dict) or not condition:
            raise RuleEngineError(message='trigger_condition 缺少 when 条件树')

        scope = raw.get('scope') or 'clause'
        if scope not in ('clause', 'document'):
            raise RuleEngineError(message=f'不支持的 scope：{scope}')

        risk_type = raw.get('risk_type') or 'high_risk_exp'
        return scope, risk_type, condition

    def _build_document_context(self, clauses: List[Dict[str, Any]],
                                meta: Dict[str, Any]) -> Dict[str, Any]:
        """
        【辅助函数】构建文档级上下文
        功能：把「整份合同」层面的派生素材给规则用（条款类型清单、条款数等）
        :param clauses: 条款列表
        :param meta: 元数据
        :return: 文档级上下文字典
        """
        clause_types = [clause.get('clause_type') for clause in clauses if clause.get('clause_type')]
        return {
            'clause_types': clause_types,
            'clause_count': len(clauses),
            'has_party_b': bool(meta.get('party_b')),
            'has_credit_code': bool(meta.get('credit_code')),
        }

    def _evaluate_condition(self, node: Dict[str, Any], context: Dict[str, Any]) -> bool:
        """
        【辅助函数】递归求值条件树
        :param node: 条件节点（逻辑节点或叶子节点）
        :param context: 求值上下文 {'clause':..., 'meta':..., 'document':...}
        :return: 是否命中
        """
        if not isinstance(node, dict):
            raise RuleEngineError(message=f'条件节点必须是字典，实际是 {type(node).__name__}')

        # 1.逻辑节点
        if 'and' in node:
            children = node['and']
            if not isinstance(children, list):
                raise RuleEngineError(message='and 的值必须是列表')
            return all(self._evaluate_condition(child, context) for child in children)

        if 'or' in node:
            children = node['or']
            if not isinstance(children, list):
                raise RuleEngineError(message='or 的值必须是列表')
            return any(self._evaluate_condition(child, context) for child in children)

        if 'not' in node:
            child = node['not']
            if not isinstance(child, dict):
                raise RuleEngineError(message='not 的值必须是字典')
            return not self._evaluate_condition(child, context)

        # 2.叶子节点
        return self._evaluate_leaf(node, context)

    def _evaluate_leaf(self, node: Dict[str, Any], context: Dict[str, Any]) -> bool:
        """
        【辅助函数】求值叶子条件 {field, op, value}
        :param node: 叶子节点
        :param context: 求值上下文
        :return: 是否命中
        """
        field_name = node.get('field')
        operator = node.get('op')
        expected = node.get('value')

        if not field_name:
            raise RuleEngineError(message='叶子条件缺少 field')
        if not operator:
            raise RuleEngineError(message=f'字段 {field_name} 的条件缺少 op')
        if operator not in self._OPERATORS:
            raise RuleEngineError(
                message=f'不支持的算子 {operator}，支持：{list(self._OPERATORS)}'
            )

        actual = self._resolve_field(field_name, context)
        return self._apply_operator(operator, actual, expected, field_name)

    def _resolve_field(self, field_name: str, context: Dict[str, Any]) -> Any:
        """
        【辅助函数】解析字段值
        功能：按 clause → meta → document 的顺序查找；字段不存在返回 None
        :param field_name: 字段名
        :param context: 求值上下文
        :return: 字段值；不存在返回 None
        """
        for source in ('clause', 'meta', 'document'):
            container = context.get(source) or {}
            if field_name in container:
                return container[field_name]
        return None

    def _apply_operator(self, operator: str, actual: Any, expected: Any,
                        field_name: str) -> bool:
        """
        【辅助函数】执行算子
        :param operator: 算子名
        :param actual: 字段实际值
        :param expected: 规则里配置的期望值
        :param field_name: 字段名（错误提示用）
        :return: 是否命中
        """
        # ---------- 存在性 ----------
        if operator == 'exists':
            return not self._is_empty(actual)
        if operator == 'missing':
            return self._is_empty(actual)

        # ---------- 相等 ----------
        if operator == 'eq':
            if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
                return float(actual) == float(expected)
            return str(actual or '').strip() == str(expected or '').strip()

        # ---------- 数值比较 ----------
        if operator in ('gt', 'lt'):
            actual_number = self._to_number(actual)
            expected_number = self._to_number(expected)
            if actual_number is None or expected_number is None:
                return False  # 不是数字就不命中，而不是报错
            return actual_number > expected_number if operator == 'gt' \
                else actual_number < expected_number

        # ---------- 文本类 ----------
        text = '' if actual is None else str(actual)
        expected_text = '' if expected is None else str(expected)

        if operator == 'contains':
            return expected_text in text

        if operator in ('contains_any', 'not_contains'):
            if not isinstance(expected, list):
                raise RuleEngineError(message=f'字段 {field_name} 的 {operator} 需要列表值')
            hit = any(str(item) in text for item in expected)
            return hit if operator == 'contains_any' else not hit

        if operator in ('regex', 'not_regex'):
            if not isinstance(expected, str):
                raise RuleEngineError(message=f'字段 {field_name} 的 {operator} 需要字符串正则')
            try:
                hit = re.search(expected, text) is not None
            except re.error as e:
                raise RuleEngineError(message=f'字段 {field_name} 的正则非法：{e}')
            return hit if operator == 'regex' else not hit

        raise RuleEngineError(message=f'算子 {operator} 未实现')

    @staticmethod
    def _is_empty(value: Any) -> bool:
        """判断字段是否"缺失"：None / 空串 / 空列表 / 空字典 都算缺失"""
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        if isinstance(value, (list, dict, tuple, set)):
            return len(value) == 0
        return False

    @staticmethod
    def _to_number(value: Any) -> Optional[float]:
        """尽力把值转成数字；转不了返回 None"""
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None

    def _build_risk_item(self, rule: Dict[str, Any], risk_type: str,
                         clause: Optional[Dict[str, Any]], meta: Dict[str, Any],
                         scope: str) -> Dict[str, Any]:
        """
        【辅助函数】把命中的规则翻译成一条风险项
        :param rule: 规则字典
        :param risk_type: 风险类型
        :param clause: 命中的条款（文档级规则为 None）
        :param meta: 元数据
        :param scope: 求值范围
        :return: 风险项字典
        """
        evidence = self._build_evidence(scope, clause, meta, rule)
        cause = f'{rule["code"]} {rule["name"]}'
        if evidence:
            cause = f'{cause}：命中「{evidence[:EVIDENCE_IN_CAUSE_MAX]}」'

        return {
            'rule_code': rule['code'],
            'matched_rule_id': rule['id'],
            # 展示用条款编号（前端联动锚点）
            'clause_id': (clause or {}).get('clause_id', ''),
            # 数据库外键（risk_item.clause_id 要用它）
            'db_clause_id': (clause or {}).get('db_clause_id'),
            'page_no': (clause or {}).get('page_no', 0),
            'paragraph_index': (clause or {}).get('paragraph_index', 0),
            'dimension': rule.get('dimension'),
            'risk_type': risk_type,
            'risk_level': rule.get('risk_level'),
            'cause': cause[:512],
            'evidence': evidence,
            'legal_basis': rule.get('legal_basis'),
            'template_clause_id': rule.get('template_clause_id'),
            'source': RiskSource.RULE,
        }

    def _build_evidence(self, scope: str, clause: Optional[Dict[str, Any]],
                        meta: Dict[str, Any], rule: Dict[str, Any]) -> str:
        """
        【辅助函数】提取风险证据片段
        功能：条款级规则取条款正文；文档级规则说明是哪个元数据字段缺失/异常
        :return: 证据片段（已做压缩与截断，便于放进 cause 列）
        """
        if scope == 'document':
            missing = [
                name for name in ('party_b', 'credit_code')
                if self._is_empty(meta.get(name))
            ]
            if missing:
                return f'元数据缺失：{"、".join(missing)}'
            party_b = str(meta.get('party_b') or '')
            return party_b[:EVIDENCE_IN_CAUSE_MAX]

        content = (clause or {}).get('content') or ''
        # 压掉换行与多余空白，让证据片段在单行里可读
        return re.sub(r'\s+', ' ', content).strip()

    def _step_4_print_stats(self, rules: List[Dict[str, Any]], risk_items: List[Dict[str, Any]],
                            skipped_rules: List[str], matched_rule_count: int,
                            elapsed_ms: float) -> None:
        """
        【步骤4】输出统计日志
        """
        level_counter: Dict[str, int] = {}
        for item in risk_items:
            level = item.get('risk_level') or 'none'
            level_counter[level] = level_counter.get(level, 0) + 1

        self.log_step('载入规则', str(len(rules)))
        self.log_step('命中规则', str(matched_rule_count))
        self.log_step('风险项数', str(len(risk_items)))
        self.log_step('等级分布', json.dumps(level_counter, ensure_ascii=False))
        self.log_step('评估耗时', f'{elapsed_ms:.1f} ms')

        if skipped_rules:
            self.log_step('跳过规则', str(skipped_rules))

        # SPEC 9.3：单任务全部规则评估目标耗时 < 50ms
        if elapsed_ms > self.config.rule_eval_warn_ms:
            self.logger.warning(
                f'规则评估耗时 {elapsed_ms:.1f} ms 超出目标 '
                f'{self.config.rule_eval_warn_ms} ms（SPEC 9.3）'
            )


if __name__ == '__main__':
    # 激活日志的全局配置
    setup_logging()

    import tempfile
    from pathlib import Path

    from db.seed import seed_all
    from db.session import init_db, init_engine, reset_engine

    node_rule_match = NodeRuleMatch()
    passed = 0

    # 临时库 + 写入 SPEC 的 R-01~R-08 种子
    work_dir = Path(tempfile.mkdtemp(prefix='node_rule_match_test_'))
    reset_engine()
    init_engine(f'sqlite:///{work_dir.as_posix()}/rule_test.db')
    init_db()
    seed_all()
    reset_rule_cache()

    def _clause(clause_type: str, content: str, seq: int = 0) -> Dict[str, Any]:
        """构造一条条款"""
        return {
            'clause_id': f'cl_{seq + 1:03d}',
            'db_clause_id': seq + 1,
            'seq': seq,
            'clause_type': clause_type,
            'title': content[:20],
            'content': content,
            'start_offset': seq * 100,
            'end_offset': seq * 100 + len(content),
            'page_no': 1,
            'paragraph_index': seq,
        }

    def _run(clauses: List[Dict[str, Any]], meta: Dict[str, Any]) -> List[Dict[str, Any]]:
        """跑一遍规则引擎"""
        state = get_default_state()
        state.update({
            'task_id': '1',
            'clauses': clauses,
            'meta': meta,
            'contract_type': '采购',
        })
        return node_rule_match(state)['rule_risk_items']

    def _codes(items: List[Dict[str, Any]]) -> List[str]:
        """取出命中的规则编码集合"""
        return sorted({item['rule_code'] for item in items})

    # ---------- 一、逐条验证 SPEC 的 R-01~R-08 ----------
    logging.getLogger().info('=' * 18 + ' 一、R-01~R-08 逐条验证 ' + '=' * 18)

    full_meta = {
        'party_a': '某某科技有限公司',
        'party_b': '某某软件股份有限公司',
        'credit_code': '91310000MA1FL1234X',
        'amount': 1280000.0,
    }

    rule_cases = [
        # (说明, 条款, 元数据, 期望命中的规则, 期望不命中的规则)
        ('R-02 违约无上限',
         [_clause('breach', '任何一方违约的，甲方承担全部损失，赔偿责任不设赔偿上限。')],
         full_meta, ['R-02'], ['R-07']),
        ('R-03 管辖不利己方',
         [_clause('jurisdiction', '协商不成的，提交乙方所在地人民法院管辖。')],
         full_meta, ['R-03'], []),
        ('R-04 付款无验收前置',
         [_clause('payment', '货到即付全款，未设验收条件。')],
         full_meta, ['R-04'], []),
        ('R-05 保密无期限',
         [_clause('confidentiality', '双方对商业秘密承担保密义务。')],
         full_meta, ['R-05'], []),
        ('R-06 不可抗力无时效',
         [_clause('force_majeure', '发生不可抗力的，双方互不承担责任。')],
         full_meta, ['R-06'], []),
        ('R-07 违约金比例超限',
         [_clause('breach', '乙方逾期交货的，应按合同总金额的 30% 支付违约金。')],
         full_meta, ['R-07'], ['R-02']),
        ('R-08 IP 归属不利',
         [_clause('ip', '本合同软件的知识产权归乙方所有。')],
         full_meta, ['R-08'], []),
        ('R-01 主体缺失（文档级）',
         [_clause('subject', '双方确认具备签约资格。')],
         {'party_a': '某某科技有限公司'}, ['R-01'], []),
    ]

    for case_name, clauses, meta, expected_hit, expected_miss in rule_cases:
        items = _run(clauses, meta)
        codes = _codes(items)
        for code in expected_hit:
            assert code in codes, f'{case_name}：应命中 {code}，实际命中 {codes}'
        for code in expected_miss:
            assert code not in codes, f'{case_name}：不应命中 {code}，实际命中 {codes}'
        passed += 1
        logging.getLogger().info(f'  ✅ {case_name:<20} → 命中 {codes}')

    # ---------- 二、正向用例：合规条款不得误报 ----------
    logging.getLogger().info('=' * 18 + ' 二、合规条款不误报 ' + '=' * 18)

    clean_cases = [
        ('保密写明 5 年', [_clause('confidentiality', '双方的保密义务自本合同生效之日起至终止后 5 年内持续有效。')], 'R-05'),
        ('不可抗力写明 15 日', [_clause('force_majeure', '遭受不可抗力一方应在 15 日内书面通知对方。')], 'R-06'),
        ('违约金 10%', [_clause('breach', '乙方逾期交货的，应按合同总金额的 10% 支付违约金。')], 'R-07'),
        ('付款有验收前置', [_clause('payment', '甲方应在验收合格后 15 个工作日内支付合同款项。')], 'R-04'),
        ('管辖约定甲方所在地', [_clause('jurisdiction', '协商不成的，提交甲方所在地人民法院管辖。')], 'R-03'),
    ]
    for case_name, clauses, should_not_hit in clean_cases:
        items = _run(clauses, full_meta)
        codes = _codes(items)
        assert should_not_hit not in codes, f'{case_name}：不应命中 {should_not_hit}，实际 {codes}'
        passed += 1
        logging.getLogger().info(f'  ✅ {case_name:<20} → 未误报 {should_not_hit}')

    # ---------- 三、算子逐个验证 ----------
    logging.getLogger().info('=' * 18 + ' 三、算子验证 ' + '=' * 18)

    operator_cases = [
        ({'field': 'clause_type', 'op': 'eq', 'value': 'payment'}, True),
        ({'field': 'clause_type', 'op': 'eq', 'value': 'breach'}, False),
        ({'field': 'content', 'op': 'contains', 'value': '验收'}, True),
        ({'field': 'content', 'op': 'contains_any', 'value': ['验收', '不存在的词']}, True),
        ({'field': 'content', 'op': 'contains_any', 'value': ['不存在的词A', '不存在的词B']}, False),
        ({'field': 'content', 'op': 'not_contains', 'value': ['不存在的词']}, True),
        # 正则命中：'未设验收条件' 命中「未.*验收」（这是补过反向分支后的宽带）
        ({'field': 'content', 'op': 'regex', 'value': '验收.*未|未.*验收|无.*验收|验收.*无'}, True),
        # SPEC 9.2 的窄写法漏掉反向语序，应如实返回不命中
        ({'field': 'content', 'op': 'regex', 'value': '验收.*未|无.*验收'}, False),
        ({'field': 'content', 'op': 'regex', 'value': '保密期.*年'}, False),
        ({'field': 'content', 'op': 'not_regex', 'value': '保密期.*年'}, True),
        ({'field': 'party_b', 'op': 'exists', 'value': None}, True),
        ({'field': 'amount', 'op': 'exists', 'value': None}, True),
        ({'field': 'credit_code', 'op': 'missing', 'value': None}, False),
        ({'field': 'amount', 'op': 'gt', 'value': 1000000}, True),
        ({'field': 'amount', 'op': 'lt', 'value': 1000000}, False),
        ({'field': 'amount', 'op': 'gt', 'value': 9999999}, False),
        # 非数字字段做数值比较 → 不命中（而不是报错）
        ({'field': 'party_b', 'op': 'gt', 'value': 1}, False),
    ]
    context = {
        'clause': _clause('payment', '货到即付全款，未设验收条件。'),
        'meta': full_meta,
        'document': {},
    }
    for leaf, expected in operator_cases:
        actual = node_rule_match._evaluate_leaf(leaf, context)
        assert actual is expected, f"算子用例 {leaf} 期望 {expected}，实际 {actual}"
        passed += 1
    logging.getLogger().info(f'  ✅ {len(operator_cases)} 个算子用例全部通过')

    # 逻辑组合
    logic_context = {'clause': _clause('payment', '货到即付全款。'), 'meta': {}, 'document': {}}
    assert node_rule_match._evaluate_condition(
        {'and': [{'field': 'clause_type', 'op': 'eq', 'value': 'payment'},
                 {'field': 'content', 'op': 'contains', 'value': '货到'}]}, logic_context), \
        'and：两个条件都成立时应命中'
    assert not node_rule_match._evaluate_condition(
        {'and': [{'field': 'clause_type', 'op': 'eq', 'value': 'breach'},
                 {'field': 'content', 'op': 'contains', 'value': '货到'}]}, logic_context), \
        'and：任一条件不成立时应不命中'
    assert node_rule_match._evaluate_condition(
        {'or': [{'field': 'clause_type', 'op': 'eq', 'value': 'breach'},
                {'field': 'content', 'op': 'contains', 'value': '货到'}]}, logic_context), \
        'or：任一条件成立即应命中'
    assert node_rule_match._evaluate_condition(
        {'not': {'field': 'clause_type', 'op': 'eq', 'value': 'breach'}}, logic_context), \
        'not：取反应命中'
    passed += 1
    logging.getLogger().info('  ✅ and / or / not 逻辑组合正确')

    # ---------- 四、风险项字段完整性 ----------
    logging.getLogger().info('=' * 18 + ' 四、风险项字段 ' + '=' * 18)

    items = _run([_clause('ip', '本合同软件的知识产权归乙方所有。', seq=3)], full_meta)
    assert len(items) == 1
    item = items[0]
    assert item['rule_code'] == 'R-08'
    assert item['risk_level'] == 'high', f"风险等级应为 high，实际 {item['risk_level']}"
    assert item['dimension'] == 'ip'
    assert item['source'] == 'rule', '规则层产出的 source 必须是 rule'
    assert item['matched_rule_id'] is not None, '必须记录命中规则ID（风险→依据闭环）'
    assert item['legal_basis'], '必须带合规依据（SPEC 4.3.1 第 3 层证据链）'
    assert item['clause_id'] == 'cl_004', '应记录展示用条款编号'
    assert item['db_clause_id'] == 4, '应记录数据库条款主键'
    assert item['page_no'] == 1, '应带页码，供前端跳页'
    assert item['evidence'], '应带证据片段'
    assert item['template_clause_id'] is not None, '应带推荐模板ID（风险→建议闭环）'
    passed += 1
    logging.getLogger().info('  ✅ 风险项字段完整（依据/模板/证据/双ID/页码）')

    # ---------- 五、健壮性：坏规则不能拖垮整次审查 ----------
    logging.getLogger().info('=' * 18 + ' 五、坏规则容错 ' + '=' * 18)

    from db.models import Rule as RuleModel

    with session_scope() as session:
        session.add_all([
            RuleModel(code='R-90', name='未知算子规则', dimension='breach', risk_level='high',
                      trigger_condition={'when': {'field': 'content', 'op': '不存在的算子',
                                                  'value': 'x'}}, enabled=True, priority=900),
            RuleModel(code='R-91', name='非法正则规则', dimension='breach', risk_level='high',
                      trigger_condition={'when': {'field': 'content', 'op': 'regex',
                                                  'value': '[未闭合'}}, enabled=True, priority=901),
            RuleModel(code='R-92', name='已停用规则', dimension='breach', risk_level='high',
                      trigger_condition={'when': {'field': 'clause_type', 'op': 'eq',
                                                  'value': 'breach'}}, enabled=False, priority=902),
        ])
    reset_rule_cache()

    items = _run([_clause('breach', '违约金按合同总金额的 10% 支付。')], full_meta)
    codes = _codes(items)
    assert 'R-90' not in codes and 'R-91' not in codes, '坏规则不应产出风险项'
    assert 'R-92' not in codes, '停用规则不应参与求值'
    passed += 1
    logging.getLogger().info('  ✅ 未知算子/非法正则/停用规则均被正确跳过，未影响其他规则')

    # ---------- 六、条款为空时文档级规则仍执行 ----------
    logging.getLogger().info('=' * 18 + ' 六、条款为空 ' + '=' * 18)

    items = _run([], {'party_a': '某某科技有限公司'})
    codes = _codes(items)
    assert 'R-01' in codes, '条款为空时文档级规则 R-01 仍应执行（否则主体缺失的风险会漏检）'
    assert all(item['clause_id'] == '' for item in items), '文档级风险项不应挂条款'
    passed += 1
    logging.getLogger().info('  ✅ 条款为空 → 文档级 R-01 仍正确检出')

    # ---------- 七、评估耗时（SPEC 9.3 目标 < 50ms）----------
    logging.getLogger().info('=' * 18 + ' 七、评估耗时 ' + '=' * 18)

    many_clauses = [
        _clause('breach', f'第{i}条 违约责任：双方应协商解决。', seq=i) for i in range(50)
    ]
    started = time.perf_counter()
    _run(many_clauses, full_meta)
    elapsed = (time.perf_counter() - started) * 1000
    logging.getLogger().info(f'  ✅ 50 条条款 × 11 条规则，评估耗时 {elapsed:.1f} ms（规则部分纯计算）')
    passed += 1

    reset_engine()
    logging.getLogger().info(f'node_rule_match 自测全部通过 ✅ 共 {passed} 个用例')
