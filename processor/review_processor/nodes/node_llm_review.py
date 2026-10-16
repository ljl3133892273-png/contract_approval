'''processor/review_processor/nodes/node_llm_review.py'''

'''
步骤分解
1. 接收状态: 取 `clauses` / `meta` / `contract_type` / `rule_risk_items`。
2. 挑选目标: 默认只审查「未被规则命中」的条款（SPEC 4.3.1 第 2 层的原话）。
3. 并发审查: 按条款并发调用 LLM（限并发可配），超长条款按段落二次切分后分段审查再合并。
4. 校验归一: 逐条校验模型返回的结构，非法值归一化，没有成因的条目直接丢弃。
5. 失败降级: 累计失败达到阈值 → 置 llm_failed（触发 REVIEW_FAIL），**但规则层结果原样保留**。
6. 写回状态 + 输出统计日志。

需求分析
输入:
- `clauses` / `meta` / `contract_type`：来自 node_load_clause
- `rule_risk_items`：规则层已命中的风险（用于避让与统计）

输出:
- `llm_risk_items`：LLM 层产出的风险项
- `llm_is_mock`：结果是否来自 mock（前端据此提示"深度语义审查未完成"）
- `llm_failed` / `llm_fail_count`：是否整体失败及失败次数

降级策略（SPEC 8.5，必须严格遵守）:
    规则引擎结果**始终保留并优先展示**。LLM 失败时任务仍可完成，
    只是工作台要提示"深度语义审查未完成"。
    → 所以本节点**永不抛异常给图**，所有 LLM 故障都转成 `llm_failed` 状态位。
    这与解析子图"失败就进 blocked"的策略不同，是 SPEC 明确要求的差异。

关于"连续失败":
    并发调用下无法严格保证"连续"语义（完成顺序不确定），
    因此这里按**累计失败数**达到阈值即判定 LLM 不可用。
    实际场景中 LLM 故障基本都是系统性的（key 失效、端点不通），
    累计与连续的结果一致。

关于并发:
    SPEC 13 写的是 asyncio.gather 限并发 4。但本项目的 LangGraph 节点是**同步**的，
    所以用 ThreadPoolExecutor 达到同等效果（LLM 调用是 IO 密集，线程池足够）。
'''

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from db.constants import RiskLevel, RiskSource
from processor.review_processor.base import BaseNode, setup_logging
from processor.review_processor.exceptions import LLMError, ReviewProcessError
from processor.review_processor.prompt.review_prompt import (
    DIMENSIONS,
    RISK_LEVELS,
    RISK_TYPES,
    build_review_messages,
)
from processor.review_processor.state import ReviewGraphState, get_default_state
from utils.json_format_utils import safe_json_loads
from utils.llm_utils import get_llm_provider


class NodeLlmReview(BaseNode):
    '''LLM 语义审查节点：对规则覆盖不到的条款做深度语义研判'''
    name = 'node_llm_review'

    # 单条条款最多采纳多少条 LLM 风险项（防止模型输出失控）
    _MAX_ITEMS_PER_CLAUSE = 10
    # 默认风险类型（模型给了非法值时的兜底）
    _DEFAULT_RISK_TYPE = 'high_risk_exp'

    def __init__(self, config=None, provider=None):
        """
        初始化节点
        :param config: 配置对象，默认使用全局配置
        :param provider: LLM Provider 覆盖项（仅测试注入用）
        """
        super().__init__(config)
        self._provider_override = provider

    def _get_provider(self):
        """获取 LLM Provider（优先用注入的覆盖项，便于测试）"""
        if self._provider_override is not None:
            return self._provider_override
        return get_llm_provider()

    def process(self, state: ReviewGraphState) -> ReviewGraphState:
        """
        节点：LLM 语义审查（node_llm_review）
        整体流程：取输入 → 挑目标 → 并发审查 → 校验归一 → 失败降级 → 写回状态
        核心目的：补上规则引擎覆盖不到的语义风险，且**绝不因 LLM 故障丢掉规则层结论**
        必要参数：clauses（允许为空）
        更新参数：llm_risk_items、llm_is_mock、llm_failed、llm_fail_count

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        # =======步骤一:获取输入=======
        clauses, meta, contract_type, rule_risk_items = self._step_1_get_inputs(state)

        # =======开关：允许整体关闭 LLM 审查 =======
        if not self.config.enable_llm_review:
            self.logger.warning('REVIEW_ENABLE_LLM=false，跳过 LLM 语义审查，仅保留规则层结果')
            state['llm_risk_items'] = []
            state['llm_is_mock'] = False
            state['llm_failed'] = False
            state['llm_fail_count'] = 0
            return state

        # =======步骤二:挑选审查目标=======
        targets = self._step_2_select_targets(clauses, rule_risk_items)

        provider = self._get_provider()
        is_mock = bool(getattr(provider, 'is_mock', False))
        if is_mock:
            self.logger.warning(
                'LLM 当前为 mock 实现，深度语义审查不会产出真实结论（SPEC 8.5 的降级形态）'
            )

        # =======步骤三:并发审查=======
        risk_items, fail_count = self._step_3_review_targets(
            targets, meta, contract_type, rule_risk_items, provider
        )

        # =======步骤四:判定是否整体失败========
        llm_failed = fail_count >= self.config.llm_fail_threshold

        # =======步骤五:写回状态 + 输出统计日志=======
        state['llm_risk_items'] = risk_items
        state['llm_is_mock'] = is_mock
        state['llm_failed'] = llm_failed
        state['llm_fail_count'] = fail_count

        self._step_5_print_stats(
            clauses, targets, risk_items, is_mock, fail_count, llm_failed
        )

        return state

    def _step_1_get_inputs(self, state: ReviewGraphState) -> Tuple[
            List[Dict[str, Any]], Dict[str, Any], str, List[Dict[str, Any]]]:
        """
        【步骤1】获取输入
        :param state: 项目状态字典（ReviewGraphState）
        :return: (条款列表, 元数据, 合同类型, 规则层风险项)
        """
        return (
            state.get('clauses') or [],
            state.get('meta') or {},
            state.get('contract_type') or '*',
            state.get('rule_risk_items') or [],
        )

    def _step_2_select_targets(self, clauses: List[Dict[str, Any]],
                               rule_risk_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        【步骤2】挑选审查目标
        功能：默认跳过「已被规则命中」的条款与空正文条款
        :param clauses: 全部条款
        :param rule_risk_items: 规则层风险项
        :return: 待送 LLM 的条款列表
        """
        # 空正文条款没有审查意义
        candidates = [clause for clause in clauses if (clause.get('content') or '').strip()]

        if not self.config.llm_review_only_unmatched:
            return candidates

        matched_ids = {
            item.get('db_clause_id') for item in rule_risk_items
            if item.get('db_clause_id') is not None
        }
        return [
            clause for clause in candidates
            if clause.get('db_clause_id') not in matched_ids
        ]

    def _step_3_review_targets(self, targets: List[Dict[str, Any]], meta: Dict[str, Any],
                               contract_type: str, rule_risk_items: List[Dict[str, Any]],
                               provider) -> Tuple[List[Dict[str, Any]], int]:
        """
        【步骤3】并发审查
        功能：按条款并发调用 LLM；任一调用失败只记失败数，不影响其他条款，
             达到阈值后剩余的条款直接跳过（快速失败，省掉无意义的等待）
        :param targets: 待审查条款
        :param meta: 元数据
        :param contract_type: 合同类型
        :param rule_risk_items: 规则层风险项（用于生成避让提示）
        :param provider: LLM Provider
        :return: (风险项列表, 失败次数)
        """
        if not targets:
            return [], 0

        hints_by_clause = self._build_rule_hints(rule_risk_items)

        risk_items: List[Dict[str, Any]] = []
        stopped = threading.Event()
        counter_lock = threading.Lock()
        threshold = max(1, self.config.llm_fail_threshold)
        # 用列表包一层，便于在闭包里就地累加
        fail_counter = [0]

        def _worker(clause: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[List[Dict[str, Any]]], str]:
            """单个条款的审查任务，返回 (条款, 风险项 或 None, 错误信息)"""
            if stopped.is_set():
                return clause, None, '__skipped__'
            try:
                items = self._review_one_clause(
                    clause, meta, contract_type,
                    hints_by_clause.get(clause.get('db_clause_id'), []),
                    provider,
                )
                return clause, items, ''
            except Exception as e:  # 单条款失败绝不能影响其他条款
                # 计数与止损必须在 worker 内完成：
                # 若只在主线程消费结果时才判断，快速失败场景下 worker 早就把
                # 队列里的任务全领走了，止损形同虚设。
                with counter_lock:
                    fail_counter[0] += 1
                    reached = fail_counter[0] >= threshold
                if reached:
                    stopped.set()
                return clause, None, str(e)

        with ThreadPoolExecutor(max_workers=max(1, self.config.max_llm_concurrency)) as pool:
            futures = [pool.submit(_worker, clause) for clause in targets]
            for future in futures:
                clause, items, error = future.result()

                if error == '__skipped__':
                    continue

                if error:
                    self.logger.warning(
                        f'条款 {clause.get("clause_id")} 的 LLM 审查失败：{error}'
                    )
                    continue

                risk_items.extend(items or [])

        fail_count = fail_counter[0]
        if fail_count:
            self.logger.warning(f'LLM 审查共失败 {fail_count} 次（阈值 {threshold}）')

        return self._dedupe(risk_items), fail_count

    def _review_one_clause(self, clause: Dict[str, Any], meta: Dict[str, Any],
                           contract_type: str, rule_hints: List[str],
                           provider) -> List[Dict[str, Any]]:
        """
        【辅助函数】审查单条条款
        功能：超长条款按段落二次切分后分段审查，再合并结果（SPEC 8.3）
        :param clause: 条款
        :param meta: 元数据
        :param contract_type: 合同类型
        :param rule_hints: 规则避让提示
        :param provider: LLM Provider
        :return: 该条款的风险项列表
        :raises LLMError: 调用失败或返回内容无法解析
        """
        chunks = self._split_clause(clause)
        items: List[Dict[str, Any]] = []

        for chunk_index, chunk in enumerate(chunks):
            messages = build_review_messages(chunk, meta, contract_type, rule_hints)
            try:
                raw = provider.chat(messages, json_mode=True)
            except Exception as e:
                raise LLMError(
                    message=f'条款 {clause.get("clause_id")} 第 {chunk_index + 1} 段 LLM 调用失败：{e}',
                    cause=e
                )

            payload = safe_json_loads(raw, default=None)
            if payload is None:
                raise LLMError(
                    message=f'条款 {clause.get("clause_id")} 第 {chunk_index + 1} 段返回内容无法解析为 JSON'
                )

            items.extend(self._normalize_items(payload, clause))

        return items

    def _split_clause(self, clause: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        【辅助函数】按容量上限切分条款
        功能：正文不超限则原样返回；超限则按段落切块（超长单段再硬切），
             每块保留原条款的定位信息，合并结果时不会丢失 Locator
        :param clause: 原始条款
        :return: 条款块列表
        """
        content = clause.get('content') or ''
        limit = max(500, self.config.llm_max_clause_chars)

        if len(content) <= limit:
            return [clause]

        self.logger.warning(
            f'条款 {clause.get("clause_id")} 正文 {len(content)} 字超出上限 {limit}，'
            f'按段落二次切分审查'
        )

        blocks: List[str] = []
        current = ''
        for paragraph in content.split('\n'):
            # 单个段落本身就超限 → 硬切
            while len(paragraph) > limit:
                if current:
                    blocks.append(current)
                    current = ''
                blocks.append(paragraph[:limit])
                paragraph = paragraph[limit:]

            if len(current) + len(paragraph) + 1 > limit and current:
                blocks.append(current)
                current = paragraph
            else:
                current = f'{current}\n{paragraph}' if current else paragraph

        if current:
            blocks.append(current)

        chunks = []
        for index, block in enumerate(blocks):
            chunk = dict(clause)
            chunk['content'] = block
            chunk['chunk_index'] = index
            chunks.append(chunk)
        return chunks

    def _normalize_items(self, payload: Any, clause: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        【辅助函数】校验并归一化模型返回的风险项
        功能：不信任模型输出 —— 没有成因的条目直接丢弃（无可信依据），
             非法枚举值归一化为合法值并告警；最多采纳 _MAX_ITEMS_PER_CLAUSE 条
        :param payload: 解析后的 JSON
        :param clause: 归属条款
        :return: 归一化后的风险项列表
        """
        # 兼容两种返回：{"items": [...]} 或直接给数组
        if isinstance(payload, dict):
            raw_items = payload.get('items')
        elif isinstance(payload, list):
            raw_items = payload
        else:
            raw_items = None

        if not isinstance(raw_items, list):
            self.logger.warning(f'条款 {clause.get("clause_id")} 的返回缺少 items 列表，已忽略')
            return []

        normalized: List[Dict[str, Any]] = []
        for raw_item in raw_items[:self._MAX_ITEMS_PER_CLAUSE]:
            if not isinstance(raw_item, dict):
                continue

            # 1.没有成因的风险项没有可信依据 → 丢弃
            cause = str(raw_item.get('cause') or '').strip()
            if not cause:
                self.logger.warning(
                    f'条款 {clause.get("clause_id")} 有一条风险项缺少 cause，已丢弃'
                )
                continue

            # 2.风险等级必须合法，非法则降级为 low（保守起见不按高风险处理）
            risk_level = str(raw_item.get('risk_level') or '').strip().lower()
            if risk_level not in RISK_LEVELS:
                self.logger.warning(
                    f'条款 {clause.get("clause_id")} 返回非法风险等级 {risk_level!r}，已归为 low'
                )
                risk_level = RiskLevel.LOW

            # 3.维度非法则置空（库字段可空）
            dimension = str(raw_item.get('dimension') or '').strip().lower()
            if dimension and dimension not in DIMENSIONS:
                self.logger.warning(
                    f'条款 {clause.get("clause_id")} 返回非法维度 {dimension!r}，已置空'
                )
                dimension = ''

            # 4.风险类型非法则用兜底值
            risk_type = str(raw_item.get('risk_type') or '').strip().lower()
            if risk_type not in RISK_TYPES:
                risk_type = self._DEFAULT_RISK_TYPE

            normalized.append({
                'rule_code': '',
                'matched_rule_id': None,
                'clause_id': clause.get('clause_id', ''),
                'db_clause_id': clause.get('db_clause_id'),
                'page_no': clause.get('page_no', 0),
                'paragraph_index': clause.get('paragraph_index', 0),
                'dimension': dimension,
                'risk_type': risk_type,
                'risk_level': risk_level,
                'cause': cause[:512],
                'evidence': self._trim_evidence(clause.get('content') or ''),
                'legal_basis': str(raw_item.get('legal_basis') or '').strip()[:512],
                'suggestion': str(raw_item.get('suggestion') or '').strip(),
                'template_clause_id': None,
                # LLM 结果来源标记，前端与报告要能区分规则/语义
                'source': RiskSource.LLM,
            })

        return normalized

    @staticmethod
    def _trim_evidence(content: str, limit: int = 200) -> str:
        """压缩并截断证据片段"""
        return ' '.join(content.split())[:limit]

    def _build_rule_hints(self, rule_risk_items: List[Dict[str, Any]]) -> Dict[Any, List[str]]:
        """
        【辅助函数】按条款汇总规则层已命中的风险，作为 LLM 的避让提示
        :param rule_risk_items: 规则层风险项
        :return: {db_clause_id: [风险描述, ...]}
        """
        hints: Dict[Any, List[str]] = {}
        for item in rule_risk_items:
            clause_key = item.get('db_clause_id')
            if clause_key is None:
                continue
            hints.setdefault(clause_key, []).append(
                item.get('cause') or item.get('rule_code') or ''
            )
        return hints

    def _dedupe(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        【辅助函数】风险项去重
        功能：同一条款下 (维度, 类型, 成因) 完全相同的视为重复（分段审查会重复产出）
        """
        seen = set()
        deduped = []
        for item in items:
            key = (
                item.get('db_clause_id'),
                item.get('dimension'),
                item.get('risk_type'),
                item.get('cause'),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def _step_5_print_stats(self, clauses: List[Dict[str, Any]], targets: List[Dict[str, Any]],
                            risk_items: List[Dict[str, Any]], is_mock: bool,
                            fail_count: int, llm_failed: bool) -> None:
        """
        【步骤5】输出统计日志
        """
        level_counter: Dict[str, int] = {}
        for item in risk_items:
            level = item.get('risk_level') or 'low'
            level_counter[level] = level_counter.get(level, 0) + 1

        self.log_step('条款总数', str(len(clauses)))
        self.log_step('送审条款', str(len(targets)))
        self.log_step('供应商', 'mock（占位）' if is_mock else '真实 LLM')
        self.log_step('语义风险项', str(len(risk_items)))
        self.log_step('等级分布', str(level_counter))
        self.log_step('失败次数', str(fail_count))

        if llm_failed:
            self.logger.warning(
                f'LLM 失败次数达到阈值 {self.config.llm_fail_threshold}，'
                f'深度语义审查未完成 —— 规则层结果已保留，任务可继续（SPEC 8.5）'
            )


if __name__ == '__main__':
    # 激活日志的全局配置
    setup_logging()

    from processor.review_processor.config import ReviewConfig

    passed = 0

    def _clause(db_id: int, clause_type: str, content: str) -> Dict[str, Any]:
        """构造一条条款"""
        return {
            'clause_id': f'cl_{db_id:03d}',
            'db_clause_id': db_id,
            'seq': db_id - 1,
            'clause_type': clause_type,
            'title': content[:16],
            'content': content,
            'start_offset': db_id * 100,
            'end_offset': db_id * 100 + len(content),
            'page_no': 1,
            'paragraph_index': db_id - 1,
        }

    class _FakeProvider:
        """假 Provider：按预设脚本返回内容，用来驱动各种分支"""

        is_mock = False
        name = 'fake'

        def __init__(self, responses: List[Any]):
            self._responses = responses
            self.call_count = 0
            self.last_messages: List[List[Dict[str, str]]] = []

        def chat(self, messages, temperature=None, json_mode=False) -> str:
            self.last_messages.append(messages)
            index = min(self.call_count, len(self._responses) - 1)
            self.call_count += 1
            response = self._responses[index]
            if isinstance(response, Exception):
                raise response
            if callable(response):
                return response(messages)
            return response

    VALID_JSON = '''
{"items": [
  {"dimension": "payment", "risk_type": "timing", "risk_level": "high",
   "cause": "付款未设验收前置条件", "legal_basis": "《民法典》第626条",
   "suggestion": "建议增加验收合格后付款的前置条件"}
]}
'''

    # ---------- 一、mock 供应商：状态位正确，且不产出真实结论 ----------
    logging.getLogger().info('=' * 18 + ' 一、mock 供应商 ' + '=' * 18)

    from utils.llm_utils import MockLLMProvider, get_llm_config

    mock_node = NodeLlmReview(provider=MockLLMProvider(reason='测试'))
    state = get_default_state()
    state.update({
        'task_id': '1',
        'clauses': [_clause(1, 'payment', '货到即付全款。')],
        'meta': {},
        'contract_type': '采购',
    })
    result = mock_node(state)

    assert result['llm_is_mock'] is True, 'mock 供应商应置 llm_is_mock'
    assert result['llm_risk_items'] == [], 'mock 不应产出任何风险项'
    assert result['llm_failed'] is False, 'mock 是主动降级，不算失败'
    passed += 1
    logging.getLogger().info('  ✅ mock 供应商 → 无风险项、llm_is_mock=True、未标记失败')

    # ---------- 二、正常解析 ----------
    logging.getLogger().info('=' * 18 + ' 二、正常解析 ' + '=' * 18)

    fake = _FakeProvider([VALID_JSON])
    node = NodeLlmReview(provider=fake)
    state = get_default_state()
    state.update({
        'task_id': '1',
        'clauses': [_clause(1, 'payment', '货到即付全款，未设验收条件。')],
        'meta': {'party_a': '甲公司', 'party_b': '乙公司'},
        'contract_type': '采购',
    })
    result = node(state)
    items = result['llm_risk_items']

    assert len(items) == 1, f'应解析出 1 条风险项，实际 {len(items)}'
    item = items[0]
    assert item['risk_level'] == 'high'
    assert item['dimension'] == 'payment'
    assert item['risk_type'] == 'timing'
    assert item['source'] == 'llm', 'LLM 产出的 source 必须是 llm'
    assert item['db_clause_id'] == 1, '必须绑定数据库条款主键'
    assert item['clause_id'] == 'cl_001', '必须绑定展示用条款编号'
    assert item['page_no'] == 1, '必须带页码'
    assert item['legal_basis'], '应带法律依据'
    assert item['suggestion'], '应带修改建议'
    assert fake.last_messages[0][0]['role'] == 'system', '应发送 system 消息'
    passed += 1
    logging.getLogger().info('  ✅ 合法 JSON → 风险项字段完整并正确绑定条款')

    # ---------- 三、JSON 兜底：带围栏也能解析 ----------
    logging.getLogger().info('=' * 18 + ' 三、JSON 围栏兜底 ' + '=' * 18)

    fenced = _FakeProvider([f'```json\n{VALID_JSON}\n```'])
    state['clauses'] = [_clause(2, 'breach', '违约方赔偿全部损失。')]
    result = NodeLlmReview(provider=fenced)(state)
    assert len(result['llm_risk_items']) == 1, '带 markdown 围栏的返回也应能解析'
    passed += 1
    logging.getLogger().info('  ✅ 模型返回 ```json 围栏 → 解析兜底生效')

    # ---------- 四、非法值归一化 / 无效项丢弃 ----------
    logging.getLogger().info('=' * 18 + ' 四、输出校验 ' + '=' * 18)

    messy = _FakeProvider(['''
{"items": [
  {"dimension": "乱填的维度", "risk_type": "乱填类型", "risk_level": "超级高",
   "cause": "有成因的条目", "suggestion": "s"},
  {"dimension": "payment", "risk_level": "high", "cause": "", "suggestion": "无成因应被丢弃"}
]}
'''])
    state['clauses'] = [_clause(3, 'payment', '内容')]
    result = NodeLlmReview(provider=messy)(state)
    items = result['llm_risk_items']

    assert len(items) == 1, f'缺少 cause 的条目应被丢弃，实际剩 {len(items)} 条'
    assert items[0]['risk_level'] == 'low', '非法风险等级应归一化为 low（保守处理）'
    assert items[0]['dimension'] == '', '非法维度应置空'
    assert items[0]['risk_type'] == 'high_risk_exp', '非法风险类型应使用兜底值'
    passed += 1
    logging.getLogger().info('  ✅ 非法枚举归一化、缺 cause 丢弃')

    # 返回非 JSON
    broken = _FakeProvider(['完全不是 JSON 的一段话'])
    state['clauses'] = [_clause(4, 'breach', '内容')]
    result = NodeLlmReview(provider=broken, config=ReviewConfig(llm_fail_threshold=3))(state)
    assert result['llm_risk_items'] == [], '解析失败不应产出风险项'
    assert result['llm_fail_count'] == 1, '解析失败应计入失败次数'
    assert result['llm_failed'] is False, '未达阈值不应标记整体失败'
    passed += 1
    logging.getLogger().info('  ✅ 返回非 JSON → 计入失败但不产风险项')

    # ---------- 五、降级：达到阈值后不再调用，且规则结果原样保留 ----------
    logging.getLogger().info('=' * 18 + ' 五、失败降级 ' + '=' * 18)

    always_fail = _FakeProvider([RuntimeError('模拟 LLM 端点不通')])
    rule_items = [{'rule_code': 'R-04', 'db_clause_id': 99, 'risk_level': 'high',
                   'cause': 'R-04 付款无验收前置条件'}]
    state = get_default_state()
    state.update({
        'task_id': '1',
        'clauses': [_clause(i, 'payment', f'第{i}条内容') for i in range(1, 8)],
        'meta': {},
        'contract_type': '*',
        'rule_risk_items': rule_items,
    })
    # 串行（并发=1）下止损是确定性的：恰好失败 3 次后停止
    result = NodeLlmReview(
        provider=always_fail,
        config=ReviewConfig(llm_fail_threshold=3, max_llm_concurrency=1),
    )(state)

    assert result['llm_failed'] is True, '达到阈值应标记整体失败'
    assert result['llm_fail_count'] == 3, f'应恰好失败 3 次，实际 {result["llm_fail_count"]}'
    assert always_fail.call_count == 3, \
        f'达到阈值后不应继续调用 LLM，实际调用 {always_fail.call_count} 次'
    # 关键：规则层结果必须原样保留（SPEC 8.5）
    assert result['rule_risk_items'] == rule_items, 'LLM 失败绝不能动到规则层结果'
    assert result['llm_risk_items'] == []
    passed += 1
    logging.getLogger().info('  ✅ 串行下恰好失败 3 次即停止，规则层结果原样保留')

    # 并发下无法做到"精确 3 次"（多个 worker 同时起跑），但必须提前止损
    concurrent_fail = _FakeProvider([RuntimeError('模拟 LLM 端点不通')])
    result = NodeLlmReview(
        provider=concurrent_fail,
        config=ReviewConfig(llm_fail_threshold=3, max_llm_concurrency=4),
    )(state)
    assert result['llm_failed'] is True
    assert concurrent_fail.call_count < 7, \
        f'并发下也应提前止损（7 条不应全跑），实际调用 {concurrent_fail.call_count} 次'
    assert result['llm_fail_count'] == concurrent_fail.call_count, '失败次数应与调用次数一致'
    assert result['rule_risk_items'] == rule_items, '并发失败同样不能动到规则层结果'
    passed += 1
    logging.getLogger().info(
        f'  ✅ 并发下提前止损（{concurrent_fail.call_count}/7 次后停止）'
    )

    # ---------- 六、只审查未被规则命中的条款 ----------
    logging.getLogger().info('=' * 18 + ' 六、目标筛选 ' + '=' * 18)

    spy = _FakeProvider(['{"items": []}'])
    state = get_default_state()
    state.update({
        'task_id': '1',
        'clauses': [_clause(1, 'payment', '被规则命中'), _clause(2, 'breach', '未被命中')],
        'meta': {},
        'contract_type': '*',
        'rule_risk_items': [{'rule_code': 'R-04', 'db_clause_id': 1}],
    })
    result = NodeLlmReview(provider=spy)(state)
    assert spy.call_count == 1, f'只应审查 1 条未被规则命中的条款，实际调用 {spy.call_count} 次'
    assert '未被命中' in spy.last_messages[0][1]['content']
    passed += 1
    logging.getLogger().info('  ✅ 默认跳过被规则命中的条款（SPEC 4.3.1）')

    # 关闭该开关后应全覆盖
    spy_all = _FakeProvider(['{"items": []}'])
    state['task_id'] = '1'
    result = NodeLlmReview(
        provider=spy_all, config=ReviewConfig(llm_review_only_unmatched=False)
    )(state)
    assert spy_all.call_count == 2, f'关闭开关后应审查全部 2 条，实际 {spy_all.call_count} 次'
    passed += 1
    logging.getLogger().info('  ✅ 关闭 only_unmatched → 全覆盖审查')

    # 规则摘要应作为避让提示喂给模型
    # 说明：并发下消息顺序不确定，且被规则命中的条款只在"全覆盖"那次才会送审，
    #      所以要在 spy_all（覆盖了全部 2 条）的消息里找，且只要有一条带上即可。
    assert any('R-04' in message[1]['content'] for message in spy_all.last_messages), \
        '被规则命中的条款，其规则信息应作为避让提示喂给模型'
    passed += 1
    logging.getLogger().info('  ✅ 规则命中信息作为避让提示传给模型')

    # ---------- 七、整体开关关闭 ----------
    logging.getLogger().info('=' * 18 + ' 七、整体关闭 ' + '=' * 18)

    never_called = _FakeProvider([VALID_JSON])
    state = get_default_state()
    state.update({'task_id': '1', 'clauses': [_clause(1, 'breach', '内容')], 'meta': {}})
    result = NodeLlmReview(
        provider=never_called, config=ReviewConfig(enable_llm_review=False)
    )(state)
    assert never_called.call_count == 0, '开关关闭时不应调用 LLM'
    assert result['llm_risk_items'] == [] and result['llm_failed'] is False
    passed += 1
    logging.getLogger().info('  ✅ REVIEW_ENABLE_LLM=false → 完全不调用 LLM')

    # ---------- 八、超长条款按段落二次切分 ----------
    logging.getLogger().info('=' * 18 + ' 八、超长条款切分 ' + '=' * 18)

    chunk_spy = _FakeProvider(['{"items": []}'])
    long_content = '\n'.join(f'第{i}段：' + '内容' * 200 for i in range(6))  # 约 1200+ 字
    state = get_default_state()
    state.update({
        'task_id': '1',
        'clauses': [_clause(1, 'breach', long_content)],
        'meta': {},
        'contract_type': '*',
    })
    result = NodeLlmReview(
        provider=chunk_spy, config=ReviewConfig(llm_max_clause_chars=500)
    )(state)
    assert chunk_spy.call_count > 1, \
        f'超长条款应被切分后多次调用，实际调用 {chunk_spy.call_count} 次'
    # 切分后再合并，不应产生重复项
    assert len(result['llm_risk_items']) <= chunk_spy.call_count

    # 分段审查产出的同一风险应被去重
    dup_fake = _FakeProvider([VALID_JSON])
    result = NodeLlmReview(
        provider=dup_fake, config=ReviewConfig(llm_max_clause_chars=500)
    )(state)
    assert len(result['llm_risk_items']) == 1, \
        f'分段产出的同一条风险应去重为 1 条，实际 {len(result["llm_risk_items"])} 条'
    passed += 1
    logging.getLogger().info(
        f'  ✅ 超长条款切分 {chunk_spy.call_count} 段审查，重复项已去重'
    )

    # ---------- 九、条款为空 ----------
    logging.getLogger().info('=' * 18 + ' 九、条款为空 ' + '=' * 18)

    empty_spy = _FakeProvider([VALID_JSON])
    state = get_default_state()
    state.update({'task_id': '1', 'clauses': [], 'meta': {}})
    result = NodeLlmReview(provider=empty_spy)(state)
    assert empty_spy.call_count == 0, '无条款时不应调用 LLM'
    assert result['llm_risk_items'] == []
    passed += 1
    logging.getLogger().info('  ✅ 条款为空 → 不调用 LLM，正常返回')

    logging.getLogger().info(f'node_llm_review 自测全部通过 ✅ 共 {passed} 个用例')
