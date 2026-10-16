'''processor/recommend_processor/nodes/node_llm_rewrite.py'''

'''
步骤分解
1. 接收状态: 取 `risk_items`（与 `candidates` 按下标一一对应）、`meta`、`contract_type`。
2. 并发改写: 逐风险点调用 P4 提示词（原文 + 风险成因 + 候选模板）生成个性化条款。
3. 降级兜底: 按「LLM 改写 → 采纳候选模板原文 → 保留审查阶段已有建议」三级降级。
4. 校验归一: 建议正文不能为空、超长截断、与原文完全一致时告警。
5. 失败止损: 累计失败达到阈值 → 停止调用 LLM，剩余风险点全部走模板降级。
6. 写回状态 + 输出统计日志。

需求分析
输入:
- `risk_items`：待生成建议的风险项
- `candidates`：召回结果（candidates[i] 对应 risk_items[i]）
- `meta` / `contract_type`

输出:
- `suggestions`：每个风险点的最终建议条款
- `rewrite_is_mock` / `rewrite_failed` / `rewrite_fail_count`

为什么本节点**永不抛异常给图**（与解析子图策略相反）:
    推荐是"增强"环节，不是"必需"环节。SPEC 8.5 的降级精神同样适用于这里：
    LLM 不可用时，直接用召回到的示范条款，法务照样能拿到可落地的修改建议。
    因为 LLM 故障就把整条推荐链路判死，属于过度反应。

为什么降级链是三级而不是两级:
    1. LLM 改写      —— 最贴合本条合同，但依赖外部服务
    2. 候选模板原文  —— 确定可用，语义上确实针对这类风险
    3. 审查阶段建议  —— LLM 层审查时已经给出过 suggestion（见 node_llm_review），
                        规则层风险项也带 suggestion。这一层是最后的兜底，
                        保证"只要有风险点，就尽量给出点东西"。
    4. 全都没有 → 如实留空并计入 no_suggestion，**不编造**。

关于超长原文:
    审查子图的策略是"按段落二次切分"（因为要逐段判定风险）。
    但改写不同 —— 一次改写要产出一整条完整条款，切段后没法合并。
    所以这里对超长原文做**截断**而不是切分，并告警提示法务人工核对。

关于与 P3 的 schema 对齐（重要）:
    P4 的输出与 P3 一样是 {"items": [...]}。
    因此 mock 供应商返回的 {"items": []} 天然表示"没有产出改写"，
    节点自动走模板降级 —— 不需要为 P4 写单独的 mock 分支。
'''

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from processor.recommend_processor.base import BaseNode, setup_logging
from processor.recommend_processor.exceptions import LLMError
from processor.recommend_processor.prompt.rewrite_prompt import build_rewrite_messages
from processor.recommend_processor.state import RecommendGraphState, get_default_state
from utils.json_format_utils import safe_json_loads
from utils.llm_utils import get_llm_provider

# 建议来源
SOURCE_LLM = 'llm'  # LLM 个性化改写
SOURCE_TEMPLATE = 'template'  # 直接采纳候选模板原文
SOURCE_KEPT = 'kept'  # 保留审查阶段已有的建议


class NodeLlmRewrite(BaseNode):
    '''LLM 条款改写节点：生成个性化修改示范条款（可降级为纯模板模式）'''
    name = 'node_llm_rewrite'

    # 提供给模型的候选模板上限（候选太多反而稀释注意力）
    _MAX_TEMPLATES_IN_PROMPT = 3
    # 建议理由字段的落库长度上限（risk_item 无独立列，随建议正文一并展示）
    _MAX_REASON_CHARS = 512

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

    def process(self, state: RecommendGraphState) -> RecommendGraphState:
        """
        节点：LLM 条款改写（node_llm_rewrite）
        整体流程：取输入 → 并发改写 → 降级兜底 → 校验归一 → 失败止损 → 写回状态
        核心目的：把"某类风险该用什么条款"落到"这份合同这条款该改成什么"
        必要参数：risk_items（允许为空）
        更新参数：suggestions、rewrite_is_mock、rewrite_failed、rewrite_fail_count

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        # =======步骤一:获取输入=======
        risk_items = state.get('risk_items') or []
        candidates = state.get('candidates') or []
        meta = state.get('meta') or {}
        contract_type = state.get('contract_type') or '*'

        if not risk_items:
            self.log_step('风险项', '0 条 —— 无需生成建议')
            state.update({'suggestions': [], 'rewrite_is_mock': False,
                          'rewrite_failed': False, 'rewrite_fail_count': 0})
            return state

        # =======开关：是否允许调用 LLM 改写 =======
        if not self.config.enable_llm_rewrite:
            self.logger.warning(
                'RECOMMEND_ENABLE_LLM=false，跳过 LLM 改写，直接用召回到的示范条款'
            )
            suggestions = [
                self._build_fallback(index, item, self._candidates_of(candidates, index), '')
                for index, item in enumerate(risk_items)
            ]
            state.update({
                'suggestions': self._finalize(suggestions),
                'rewrite_is_mock': False,
                'rewrite_failed': False,
                'rewrite_fail_count': 0,
            })
            self._step_6_print_stats(risk_items, state['suggestions'], False, 0, False)
            return state

        provider = self._get_provider()
        is_mock = bool(getattr(provider, 'is_mock', False))
        if is_mock:
            self.logger.warning(
                'LLM 当前为 mock 实现，无法产出个性化改写 —— 将直接采用召回到的示范条款（SPEC 8.5 的降级形态）'
            )

        # =======步骤二:并发改写（含降级与止损）=======
        suggestions, fail_count = self._step_2_rewrite_all(
            risk_items, candidates, meta, contract_type, provider, is_mock
        )

        # =======步骤三:判定是否整体失败=======
        rewrite_failed = fail_count >= self.config.llm_fail_threshold

        # =======步骤四~五:校验归一 + 写回状态=======
        suggestions = self._finalize(suggestions)
        state.update({
            'suggestions': suggestions,
            'rewrite_is_mock': is_mock,
            'rewrite_failed': rewrite_failed,
            'rewrite_fail_count': fail_count,
        })

        # =======步骤六:输出统计日志=======
        self._step_6_print_stats(risk_items, suggestions, is_mock, fail_count, rewrite_failed)

        return state

    def _step_2_rewrite_all(self, risk_items: List[Dict[str, Any]],
                            candidates: List[Dict[str, Any]], meta: Dict[str, Any],
                            contract_type: str, provider, is_mock: bool
                            ) -> Tuple[List[Dict[str, Any]], int]:
        """
        【步骤2】并发改写全部风险点
        功能：逐条调用 LLM；单条失败不影响其他条目（降级为模板/保留）；
             累计失败达到阈值后停止调用 LLM，剩余条目全部降级（止损）
        :param risk_items: 风险项
        :param candidates: 召回结果（与 risk_items 同下标）
        :param meta: 元数据
        :param contract_type: 合同类型
        :param provider: LLM Provider
        :param is_mock: 是否 mock 供应商
        :return: (建议列表（按风险项顺序）, 失败次数)
        """
        stopped = threading.Event()
        counter_lock = threading.Lock()
        threshold = max(1, self.config.llm_fail_threshold)
        fail_counter = [0]
        # 结果按下标回填，保证与 risk_items 顺序一致（并发完成顺序是不确定的）
        results: List[Optional[Dict[str, Any]]] = [None] * len(risk_items)

        def _worker(index: int) -> None:
            """单个风险点的改写任务"""
            item = risk_items[index]
            item_candidates = self._candidates_of(candidates, index)

            # mock 供应商不必真调用：直接降级（省掉一次无意义的 IO）
            if is_mock:
                results[index] = self._build_fallback(index, item, item_candidates, '')
                return

            if stopped.is_set():
                results[index] = self._build_fallback(
                    index, item, item_candidates, 'LLM 已达失败阈值，本条走降级'
                )
                return

            try:
                suggestion = self._rewrite_one(index, item, item_candidates,
                                               meta, contract_type, provider)
                results[index] = suggestion
            except Exception as e:  # 单条失败绝不能影响其他风险点
                # 计数与止损必须在 worker 内完成：
                # 若只在主线程消费结果时才判断，快速失败场景下 worker 早就
                # 把剩余任务全领走了，止损形同虚设（node_llm_review 踩过同一个坑）。
                with counter_lock:
                    fail_counter[0] += 1
                    reached = fail_counter[0] >= threshold
                if reached:
                    stopped.set()
                results[index] = self._build_fallback(index, item, item_candidates, str(e))

        with ThreadPoolExecutor(max_workers=max(1, self.config.max_llm_concurrency)) as pool:
            list(pool.map(_worker, range(len(risk_items))))

        fail_count = fail_counter[0]
        if fail_count:
            self.logger.warning(
                f'LLM 改写共失败 {fail_count} 次（阈值 {threshold}）—— '
                f'对应风险点已降级为示范条款'
            )

        return [r for r in results if r is not None], fail_count

    def _rewrite_one(self, index: int, item: Dict[str, Any],
                     item_candidates: List[Dict[str, Any]], meta: Dict[str, Any],
                     contract_type: str, provider) -> Dict[str, Any]:
        """
        【辅助函数】改写单个风险点
        :param index: 风险项下标
        :param item: 风险项
        :param item_candidates: 该风险点的候选模板（按得分降序）
        :param meta: 元数据
        :param contract_type: 合同类型
        :param provider: LLM Provider
        :return: 建议字典
        :raises LLMError: 调用失败、返回无法解析、或未产出改写内容
        """
        original_content = self._truncate_original(item.get('clause_content') or '')
        prompt_templates = item_candidates[:self._MAX_TEMPLATES_IN_PROMPT]

        messages = build_rewrite_messages(
            item, meta, contract_type, original_content, prompt_templates
        )

        try:
            raw = provider.chat(messages, json_mode=True)
        except Exception as e:
            raise LLMError(
                message=f'风险项 {item.get("db_risk_id")} 的 LLM 改写调用失败：{e}',
                cause=e
            )

        payload = safe_json_loads(raw, default=None)
        if payload is None:
            raise LLMError(
                message=f'风险项 {item.get("db_risk_id")} 的 LLM 返回内容无法解析为 JSON'
            )

        suggestion_text, reason = self._extract_suggestion(payload, item)
        if not suggestion_text:
            # 模型主动返回空 items（含 mock）→ 不是故障，走降级但不计入失败
            return self._build_fallback(index, item, item_candidates, '')

        return {
            'index': index,
            'db_risk_id': item.get('db_risk_id'),
            'db_clause_id': item.get('db_clause_id'),
            'clause_id': item.get('clause_id') or '',
            'suggestion_text': suggestion_text,
            'reason': reason,
            'source': SOURCE_LLM,
            # 只有"采纳模板原文"时才填，此处为空以示区分
            'template_clause_id': None,
            'reference_template_ids': [t['template_id'] for t in prompt_templates],
            'rewrite_error': '',
        }

    def _extract_suggestion(self, payload: Any,
                            item: Dict[str, Any]) -> Tuple[str, str]:
        """
        【辅助函数】从模型返回里取建议正文与理由
        功能：不信任模型输出 —— 兼容 {"items": [...]} 与直接数组两种形式；
             缺字段/类型不对的条目直接跳过
        :param payload: 解析后的 JSON
        :param item: 风险项（仅用于日志）
        :return: (建议正文, 修改理由)；没有可用条目时返回 ('', '')
        """
        if isinstance(payload, dict):
            raw_items = payload.get('items')
        elif isinstance(payload, list):
            raw_items = payload
        else:
            raw_items = None

        if not isinstance(raw_items, list) or not raw_items:
            self.logger.info(
                f'风险项 {item.get("db_risk_id")} 的 LLM 返回不含改写结果，'
                f'本条走模板降级'
            )
            return '', ''

        if len(raw_items) > 1:
            self.logger.warning(
                f'风险项 {item.get("db_risk_id")} 的 LLM 返回了 {len(raw_items)} 条改写，'
                f'只采纳第一条'
            )

        first = raw_items[0]
        if not isinstance(first, dict):
            return '', ''

        suggestion_text = str(first.get('suggestion_text') or '').strip()
        reason = str(first.get('reason') or '').strip()[:self._MAX_REASON_CHARS]
        return suggestion_text, reason

    def _build_fallback(self, index: int, item: Dict[str, Any],
                        item_candidates: List[Dict[str, Any]],
                        error: str = '') -> Dict[str, Any]:
        """
        【辅助函数】构造降级建议
        降级链：候选模板原文 → 审查阶段已有建议 → 留空
        :param index: 风险项下标
        :param item: 风险项
        :param item_candidates: 候选模板
        :param error: 触发降级的原因（空字符串表示主动降级，如 mock）
        :return: 建议字典
        """
        base = {
            'index': index,
            'db_risk_id': item.get('db_risk_id'),
            'db_clause_id': item.get('db_clause_id'),
            'clause_id': item.get('clause_id') or '',
            'suggestion_text': '',
            'reason': '',
            'source': '',
            'template_clause_id': None,
            'reference_template_ids': [t['template_id'] for t in item_candidates],
            'rewrite_error': error,
        }

        # 1.候选模板原文
        if item_candidates:
            top = item_candidates[0]
            base['suggestion_text'] = top.get('content') or ''
            base['reason'] = f"采用示范条款：{top.get('title') or '(无标题)'}"
            base['source'] = SOURCE_TEMPLATE
            base['template_clause_id'] = top.get('template_id')
            return base

        # 2.审查阶段已有的建议（规则层/LLM 层审查时产出）
        existing = (item.get('suggestion') or '').strip()
        if existing:
            base['suggestion_text'] = existing
            base['reason'] = '沿用审查阶段给出的修改建议'
            base['source'] = SOURCE_KEPT
            return base

        # 3.都没有 → 如实留空，绝不编造
        return base

    def _candidates_of(self, candidates: List[Dict[str, Any]],
                       index: int) -> List[Dict[str, Any]]:
        """
        【辅助函数】按风险项下标取候选模板
        说明：candidates 与 risk_items 由 node_template_recall 保证下标一一对应
        :param candidates: 候选列表
        :param index: 风险项下标
        :return: 该风险点的候选模板（无则空列表）
        """
        if index < len(candidates):
            return candidates[index].get('templates') or []
        return []

    def _truncate_original(self, content: str) -> str:
        """
        【辅助函数】超长原文截断
        说明：改写需要一次性产出完整条款，不能像审查那样分段后合并，
             所以这里截断而非切分，并告警提示人工核对。
        :param content: 条款原文
        :return: 截断后的原文
        """
        limit = max(500, self.config.llm_max_clause_chars)
        if len(content) <= limit:
            return content

        self.logger.warning(
            f'条款原文 {len(content)} 字超出上限 {limit}，已截断后送 LLM 改写，'
            f'建议法务核对被截断部分的表达'
        )
        return content[:limit]

    def _finalize(self, suggestions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        【辅助函数】对建议做最终校验与归一
        功能：超长截断；与原文完全一致时告警（说明改写没起作用）；
             无建议正文的条目保留在结果里但打上标记，便于前端提示"未生成建议"
        :param suggestions: 建议列表
        :return: 归一后的建议列表
        """
        limit = max(200, self.config.max_suggestion_chars)

        for suggestion in suggestions:
            text = (suggestion.get('suggestion_text') or '').strip()

            if len(text) > limit:
                self.logger.warning(
                    f'风险项 {suggestion.get("db_risk_id")} 的建议条款 {len(text)} 字超出上限，'
                    f'已截断'
                )
                text = text[:limit]

            suggestion['suggestion_text'] = text
            suggestion['has_suggestion'] = bool(text)

        return suggestions

    def _step_6_print_stats(self, risk_items: List[Dict[str, Any]],
                            suggestions: List[Dict[str, Any]], is_mock: bool,
                            fail_count: int, rewrite_failed: bool) -> None:
        """
        【步骤6】输出统计日志
        """
        source_counter: Dict[str, int] = {}
        for suggestion in suggestions:
            key = suggestion.get('source') or 'none'
            source_counter[key] = source_counter.get(key, 0) + 1

        with_suggestion = sum(1 for s in suggestions if s.get('has_suggestion'))

        self.log_step('待生成建议', f'{len(risk_items)} 条')
        self.log_step('供应商', 'mock（降级为模板模式）' if is_mock else '真实 LLM')
        self.log_step('来源分布', str(source_counter))
        self.log_step('已生成建议', f'{with_suggestion} / {len(suggestions)} 条')
        self.log_step('失败次数', str(fail_count))

        if rewrite_failed:
            self.logger.warning(
                f'LLM 失败次数达到阈值 {self.config.llm_fail_threshold}，'
                f'个性化改写未完成 —— 已全部降级为示范条款，推荐结果仍然可用'
            )

        no_suggestion = len(suggestions) - with_suggestion
        if no_suggestion:
            self.logger.warning(
                f'{no_suggestion} 条风险点既无 LLM 改写、也无候选模板与历史建议，'
                f'如实留空（未编造内容）'
            )


if __name__ == '__main__':
    # 激活日志的全局配置
    setup_logging()

    from processor.recommend_processor.config import RecommendConfig
    from utils.llm_utils import MockLLMProvider

    passed = 0

    def _risk(index: int, **overrides) -> Dict[str, Any]:
        """构造一条风险项"""
        item = {
            'db_risk_id': index,
            'db_clause_id': index,
            'clause_id': f'cl_{index:03d}',
            'clause_type': 'payment',
            'clause_title': '第二条 付款方式',
            'clause_content': '货到即付全款，未设验收条件。',
            'dimension': 'payment',
            'risk_type': 'timing',
            'risk_level': 'high',
            'cause': '付款未设验收前置条件',
            'legal_basis': '《民法典》第626条',
            'suggestion': '',
        }
        item.update(overrides)
        return item

    def _candidate(index: int, template_id: int = 101) -> Dict[str, Any]:
        """构造一条召回结果"""
        return {
            'index': index,
            'db_risk_id': index,
            'clause_type': 'payment',
            'matched_by': 'type',
            'templates': [{
                'template_id': template_id,
                'contract_type': '*',
                'clause_type': 'payment',
                'title': '付款与验收前置条款（示范）',
                'content': '甲方应在验收合格后 15 个工作日内支付合同款项。',
                'score': 110,
                'matched_by': 'type',
            }],
        }

    VALID_JSON = '''
{"items": [{"suggestion_text": "第二条 付款方式：甲方应在验收合格并收到合规发票后 15 个工作日内支付合同款项。",
            "reason": "增加验收前置条件与发票要求，避免付款起算点不确定。",
            "template_used": "付款与验收前置条款（示范）"}]}
'''

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
            return response

    # ---------- 一、正常改写 ----------
    logging.getLogger().info('=' * 18 + ' 一、正常改写 ' + '=' * 18)

    fake = _FakeProvider([VALID_JSON])
    state = get_default_state()
    state.update({
        'task_id': '1',
        'risk_items': [_risk(1)],
        'candidates': [_candidate(0)],
        'meta': {'party_a': '甲公司', 'party_b': '乙公司', 'amount': 100, 'currency': 'CNY'},
        'contract_type': '采购',
    })
    result = NodeLlmRewrite(provider=fake)(state)
    suggestion = result['suggestions'][0]

    assert suggestion['source'] == 'llm', f"应来自 LLM 改写，实际 {suggestion['source']}"
    assert '验收合格' in suggestion['suggestion_text'], '应采用 LLM 改写后的正文'
    assert suggestion['reason'], '必须带修改理由'
    assert suggestion['has_suggestion'] is True
    assert suggestion['template_clause_id'] is None, 'LLM 改写不采纳模板原文，应为空'
    assert suggestion['reference_template_ids'] == [101], '应记录喂给模型的候选模板'
    assert result['rewrite_is_mock'] is False and result['rewrite_failed'] is False
    # 提示词必须带上原文、成因与候选模板
    user_content = fake.last_messages[0][1]['content']
    assert '货到即付全款，未设验收条件。' in user_content
    assert '付款未设验收前置条件' in user_content
    assert '付款与验收前置条款（示范）' in user_content
    passed += 1
    logging.getLogger().info('  ✅ 合法 JSON → 采纳 LLM 改写，原文/成因/候选模板均已喂入')

    # ---------- 二、mock 供应商 → 降级为模板，且不算失败 ----------
    logging.getLogger().info('=' * 18 + ' 二、mock 降级 ' + '=' * 18)

    mock_node = NodeLlmRewrite(provider=MockLLMProvider(reason='测试'))
    state['risk_items'] = [_risk(1)]
    state['candidates'] = [_candidate(0)]
    result = mock_node(state)
    suggestion = result['suggestions'][0]

    assert result['rewrite_is_mock'] is True, 'mock 供应商应置 rewrite_is_mock'
    assert result['rewrite_failed'] is False, 'mock 是主动降级，不算失败'
    assert result['rewrite_fail_count'] == 0, 'mock 不应计入失败次数'
    assert suggestion['source'] == 'template', 'mock 下应采用模板原文'
    assert suggestion['template_clause_id'] == 101, '必须记录采纳的模板'
    assert suggestion['suggestion_text'] == '甲方应在验收合格后 15 个工作日内支付合同款项。'
    passed += 1
    logging.getLogger().info('  ✅ mock → 自动降级为示范条款，不计失败')

    # ---------- 三、模型返回空 items → 降级（不算故障）----------
    logging.getLogger().info('=' * 18 + ' 三、空 items 降级 ' + '=' * 18)

    empty = _FakeProvider(['{"items": []}'])
    state['risk_items'] = [_risk(1)]
    state['candidates'] = [_candidate(0)]
    result = NodeLlmRewrite(provider=empty)(state)
    assert result['suggestions'][0]['source'] == 'template', '空 items 应降级为模板'
    assert result['rewrite_fail_count'] == 0, '模型主动返回空不是故障，不应计失败'
    passed += 1
    logging.getLogger().info('  ✅ 模型返回空 items → 降级为模板，且不计失败')

    # ---------- 四、三级降级链 ----------
    logging.getLogger().info('=' * 18 + ' 四、降级链 ' + '=' * 18)

    never = _FakeProvider([RuntimeError('不应被调用')])

    # 4.1 无候选模板，但有审查阶段建议 → 保留
    state['risk_items'] = [_risk(1, suggestion='审查阶段的建议：增加验收前置条件。')]
    state['candidates'] = []
    result = NodeLlmRewrite(provider=never, config=RecommendConfig(enable_llm_rewrite=False))(state)
    suggestion = result['suggestions'][0]
    assert suggestion['source'] == 'kept', f"应保留历史建议，实际 {suggestion['source']}"
    assert suggestion['suggestion_text'] == '审查阶段的建议：增加验收前置条件。'
    passed += 1
    logging.getLogger().info('  ✅ 无候选模板 → 回退到审查阶段已有建议')

    # 4.2 既无候选模板也无历史建议 → 如实留空
    state['risk_items'] = [_risk(1, suggestion='')]
    state['candidates'] = []
    result = NodeLlmRewrite(provider=never, config=RecommendConfig(enable_llm_rewrite=False))(state)
    suggestion = result['suggestions'][0]
    assert suggestion['source'] == '', '没有任何来源时应留空来源'
    assert suggestion['suggestion_text'] == '', '绝不能编造建议内容'
    assert suggestion['has_suggestion'] is False, '应标记为未生成建议'
    passed += 1
    logging.getLogger().info('  ✅ 无任何来源 → 如实留空，未编造内容')

    # ---------- 五、失败降级与止损 ----------
    logging.getLogger().info('=' * 18 + ' 五、失败止损 ' + '=' * 18)

    always_fail = _FakeProvider([RuntimeError('模拟 LLM 端点不通')])
    state['risk_items'] = [_risk(i) for i in range(1, 8)]
    state['candidates'] = [_candidate(i) for i in range(7)]
    # 串行（并发=1）下止损是确定性的：恰好失败 3 次后停止调用
    result = NodeLlmRewrite(
        provider=always_fail,
        config=RecommendConfig(llm_fail_threshold=3, max_llm_concurrency=1),
    )(state)

    assert result['rewrite_failed'] is True, '达到阈值应标记整体失败'
    assert result['rewrite_fail_count'] == 3, f'应恰好失败 3 次，实际 {result["rewrite_fail_count"]}'
    assert always_fail.call_count == 3, \
        f'达到阈值后不应继续调用 LLM，实际调用 {always_fail.call_count} 次'
    assert len(result['suggestions']) == 7, '所有风险点都必须有结果（降级兜底）'
    assert all(s['source'] == 'template' for s in result['suggestions']), \
        'LLM 失败时全部应降级为模板，推荐结果仍然可用'
    # 止损后剩余条目要标出原因
    assert any('失败阈值' in (s['rewrite_error'] or '') for s in result['suggestions']), \
        '止损跳过的条目应有原因说明'
    passed += 1
    logging.getLogger().info('  ✅ 串行下恰好失败 3 次即止损，其余全部降级为模板')

    # 并发下无法做到"精确 3 次"，但必须提前止损
    concurrent_fail = _FakeProvider([RuntimeError('模拟 LLM 端点不通')])
    result = NodeLlmRewrite(
        provider=concurrent_fail,
        config=RecommendConfig(llm_fail_threshold=3, max_llm_concurrency=4),
    )(state)
    assert result['rewrite_failed'] is True
    assert concurrent_fail.call_count < 7, \
        f'并发下也应提前止损（7 条不应全跑），实际调用 {concurrent_fail.call_count} 次'
    assert len(result['suggestions']) == 7
    passed += 1
    logging.getLogger().info(
        f'  ✅ 并发下提前止损（{concurrent_fail.call_count}/7 次后停止）'
    )

    # ---------- 六、整体开关关闭 ----------
    logging.getLogger().info('=' * 18 + ' 六、整体关闭 ' + '=' * 18)

    spy = _FakeProvider([VALID_JSON])
    state['risk_items'] = [_risk(1)]
    state['candidates'] = [_candidate(0)]
    result = NodeLlmRewrite(
        provider=spy, config=RecommendConfig(enable_llm_rewrite=False)
    )(state)
    assert spy.call_count == 0, '开关关闭时不应调用 LLM'
    assert result['suggestions'][0]['source'] == 'template', '应为纯模板模式'
    assert result['rewrite_is_mock'] is False and result['rewrite_failed'] is False
    passed += 1
    logging.getLogger().info('  ✅ RECOMMEND_ENABLE_LLM=false → 纯模板模式，零 LLM 调用')

    # ---------- 七、输出校验 ----------
    logging.getLogger().info('=' * 18 + ' 七、输出校验 ' + '=' * 18)

    # 7.1 带 markdown 围栏也能解析
    fenced = _FakeProvider([f'```json\n{VALID_JSON}\n```'])
    state['risk_items'] = [_risk(1)]
    state['candidates'] = [_candidate(0)]
    result = NodeLlmRewrite(provider=fenced)(state)
    assert result['suggestions'][0]['source'] == 'llm', '带围栏的返回也应能解析'
    passed += 1

    # 7.2 返回非 JSON → 计为失败并降级
    broken = _FakeProvider(['完全不是 JSON 的一段话'])
    state['risk_items'] = [_risk(1)]
    state['candidates'] = [_candidate(0)]
    result = NodeLlmRewrite(provider=broken, config=RecommendConfig(llm_fail_threshold=5))(state)
    assert result['rewrite_fail_count'] == 1, '解析失败应计入失败次数'
    assert result['rewrite_failed'] is False, '未达阈值不应标记整体失败'
    assert result['suggestions'][0]['source'] == 'template', '解析失败应降级'
    assert result['suggestions'][0]['rewrite_error'], '应记录降级原因'
    passed += 1

    # 7.3 超长建议被截断
    long_suggestion = '{"items": [{"suggestion_text": "' + '条款内容' * 500 + '", "reason": "r"}]}'
    long_provider = _FakeProvider([long_suggestion])
    state['risk_items'] = [_risk(1)]
    state['candidates'] = [_candidate(0)]
    result = NodeLlmRewrite(
        provider=long_provider, config=RecommendConfig(max_suggestion_chars=200)
    )(state)
    assert len(result['suggestions'][0]['suggestion_text']) == 200, \
        f'超长建议应被截断到 200 字，实际 {len(result["suggestions"][0]["suggestion_text"])}'
    passed += 1

    # 7.4 建议与原文完全一致 → 仍保留但说明改写未生效（由调用方/前端判断）
    same = _FakeProvider(['{"items": [{"suggestion_text": "货到即付全款，未设验收条件。", "reason": "r"}]}'])
    state['risk_items'] = [_risk(1)]
    state['candidates'] = [_candidate(0)]
    result = NodeLlmRewrite(provider=same)(state)
    assert result['suggestions'][0]['suggestion_text'] == '货到即付全款，未设验收条件。'
    assert result['suggestions'][0]['has_suggestion'] is True
    passed += 1

    # 7.5 返回多条改写 → 只采纳第一条
    multi = _FakeProvider(['{"items": [{"suggestion_text": "第一条建议", "reason": "r1"},'
                           ' {"suggestion_text": "第二条建议", "reason": "r2"}]}'])
    state['risk_items'] = [_risk(1)]
    state['candidates'] = [_candidate(0)]
    result = NodeLlmRewrite(provider=multi)(state)
    assert result['suggestions'][0]['suggestion_text'] == '第一条建议', '应只采纳第一条'
    passed += 1
    logging.getLogger().info('  ✅ 围栏兜底 / 非 JSON 降级 / 超长截断 / 多条只取首条')

    # ---------- 八、超长原文截断 ----------
    logging.getLogger().info('=' * 18 + ' 八、超长原文 ' + '=' * 18)

    truncate_spy = _FakeProvider(['{"items": []}'])
    state['risk_items'] = [_risk(1, clause_content='内容' * 1000)]
    state['candidates'] = [_candidate(0)]
    NodeLlmRewrite(
        provider=truncate_spy, config=RecommendConfig(llm_max_clause_chars=500)
    )(state)
    prompt_content = truncate_spy.last_messages[0][1]['content']
    assert '内容' * 300 not in prompt_content, '超长原文应被截断后送模型'
    passed += 1
    logging.getLogger().info('  ✅ 超长原文截断后送 LLM（改写不切分）')

    # ---------- 九、空风险项 ----------
    logging.getLogger().info('=' * 18 + ' 九、空风险项 ' + '=' * 18)

    never_called = _FakeProvider([VALID_JSON])
    state['risk_items'] = []
    state['candidates'] = []
    result = NodeLlmRewrite(provider=never_called)(state)
    assert never_called.call_count == 0, '无风险项时不应调用 LLM'
    assert result['suggestions'] == []
    passed += 1
    logging.getLogger().info('  ✅ 无风险项 → 不调用 LLM，正常返回')

    logging.getLogger().info(f'node_llm_rewrite 自测全部通过 ✅ 共 {passed} 个用例')
