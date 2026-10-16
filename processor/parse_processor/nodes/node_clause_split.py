'''processor/parse_processor/nodes/node_clause_split.py'''

'''
步骤分解
1. 接收状态: 取 `full_text` 与 `fragments`（node_clean_text 的产出）。
2. 锚点扫描: 用条款号正则（第X条 / （一） / 1.1 / 1、）在全文里找出所有条款起点。
3. 区间切分: 按锚点把全文切成若干条款区间；锚点之前的文本（合同头/前言）单独成块。
4. 兜底处理: 一个锚点都没找到 → 整篇作为单块，**不报错、不阻塞主流程**
            （对应 SPEC 4.2.2：切分失败不阻塞，后续可按段落粒度审查）。
5. 类型归类: 关键词规则命中 → 直接定类型；未命中 → P1 LLM 辅助分类（低置信度归 other）。
6. 反查定位: 由 start_offset 反查 page_no / paragraph_index，生成完整 Locator。
7. 写回状态 + 统计日志。

需求分析
输入:
- `full_text`：去噪后全文（Locator 的全局偏移基准）
- `fragments`：DocFragment[]（提供 offset → 页码/段落的反查表）

输出:
- `clauses`：Clause[]，每项含 clause_id / clause_type / title / content /
            start_offset / end_offset / page_no / paragraph_index / seq

不变量（与 node_clean_text 一脉相承）:
    对任意条款都恒有  full_text[start_offset:end_offset] == content。
    切分时对区间做了 strip，所以偏移量做了同步修正（见 `_build_clause`）。
    这个等式是前端"点击风险卡片 → 原文高亮跳页"的唯一坐标依据。

LLM 辅助（P1，SPEC 4.2.2 辅助策略 / SPEC 8.2）:
    规则无法归类的段落调用 P1 提示词做条款分类，
    置信度低于 `ParseConfig.llm_classify_min_confidence` 一律归入 `other`。
    LLM 未配置 / mock / 异常 → 只告警并归入 other，绝不阻塞解析主链路。
'''

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from processor.parse_processor.base import BaseNode, setup_logging
from processor.parse_processor.exceptions import (
    ClauseSplitError,
    ParseProcessError,
    StateFieldError,
)
from processor.parse_processor.state import ParseGraphState, get_default_state


class NodeClauseSplit(BaseNode):
    '''条款切分节点：把全文切成带 Locator 的条款列表'''
    name = 'node_clause_split'

    # 条款标题最大字符数（超出截断，避免整段正文被当成标题）
    _TITLE_MAX_CHARS = 60

    # 标题区关键词权重：条款类型基本由标题决定，正文里的词只是佐证。
    # 若两者同权，像「付款方式：货到即付全款，未设验收条件」这种同时出现
    # 「付款」与「验收」的条款就会判错，因此标题区命中权重更高。
    _TITLE_KEYWORD_WEIGHT = 3
    # 标题区最大字符数
    _TITLE_REGION_CHARS = 30

    # 条款锚点正则（按 SPEC 4.2.2 的主策略）
    _ANCHOR_PATTERNS = (
        # 第X条 / 第 1 条（中文数字或阿拉伯数字）
        re.compile(r'第\s*[一二三四五六七八九十百零〇\d]{1,4}\s*条'),
        # （一）/ (一) —— 必须位于行首，避免命中正文里的括号
        re.compile(r'(?m)^\s*[（(]\s*[一二三四五六七八九十]{1,3}\s*[)）]'),
        # 一、/ 二、（中文数字 + 顿号，简式合同常用）—— 必须位于行首。
        # 真实语料教训：销售合同简式模板用「一、基本信息」编号，此前不识别导致整篇兜底单块。
        # 行首限定 + 中文数字后必须紧跟顿号，避免误命中正文里的「其一、」等列举。
        re.compile(r'(?m)^\s*[一二三四五六七八九十]{1,3}\s*、\s*(?=\S)'),
        # 1.1 / 1.1.1 —— 必须位于行首
        re.compile(r'(?m)^\s*\d{1,2}(?:\.\d{1,2}){1,3}\s*(?=\s|[、.])'),
        # 1、 / 1. —— 必须位于行首
        re.compile(r'(?m)^\s*\d{1,2}\s*[、.]\s*(?=\S)'),
    )

    # 条款类型关键词表（用于规则归类）
    # 说明：顺序即优先级，命中数量相同时按此顺序取靠前的类型
    _TYPE_KEYWORDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
        ('ip', ('知识产权', '著作权', '专利权', '商标', '源代码', '软件著作')),
        ('data_security', ('数据安全', '个人信息', '数据保护', '隐私', '数据合规')),
        ('confidentiality', ('保密', '商业秘密', '机密信息', '保密义务')),
        ('jurisdiction', ('争议', '管辖', '仲裁', '诉讼', '法律适用', '法院')),
        ('force_majeure', ('不可抗力',)),
        ('acceptance', ('验收', '交货', '交付', '质量标准', '检验')),
        ('payment', ('付款', '支付', '价款', '结算', '发票', '金额', '费用')),
        ('breach', ('违约', '赔偿', '滞纳金', '违约金', '损失')),
        ('term', ('合同期限', '有效期', '履行期限', '合同生效')),
        ('subject', ('标的物', '标的', '采购内容', '服务内容', '货物', '产品规格')),
    )
    DEFAULT_TYPE = 'other'

    def __init__(self, config=None, provider=None):
        """
        初始化节点
        :param config: 配置对象，默认使用全局配置
        :param provider: LLM Provider 覆盖项（仅测试注入用，P1 条款分类走它）
        """
        super().__init__(config)
        self._provider_override = provider

    def _get_provider(self):
        """获取 LLM Provider（优先用注入的覆盖项，便于测试）"""
        if self._provider_override is not None:
            return self._provider_override
        from utils.llm_utils import get_llm_provider
        return get_llm_provider()

    def process(self, state: ParseGraphState) -> ParseGraphState:
        """
        节点：条款切分（node_clause_split）
        整体流程：取输入 → 锚点扫描 → 区间切分 → 类型归类 → 反查定位 → 写回状态
        核心目的：把一整篇合同正文切成带 Locator 的条款列表，
                 让每个风险点都能指回"第几页第几段第几个字符"
        必要参数：full_text、fragments
        更新参数：clauses

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        # =======步骤一:获取输入=======
        full_text, fragments = self._step_1_get_inputs(state)

        # =======步骤二:锚点扫描=======
        # 作用：核心切分依据，找出所有条款起点（位置 + 匹配文本）
        anchors = self._step_2_find_anchors(full_text)

        # =======步骤三:按锚点切分区间=======
        # 作用：把全文切成若干区间；无锚点时走兜底，产出单块而不是报错
        segments = self._step_3_split_by_anchors(full_text, anchors)

        # =======步骤四:类型归类 + 反查定位 + 组装 Clause=======
        clauses = self._step_4_build_clauses(full_text, segments, fragments)

        # =======步骤五:写回状态 + 输出统计日志=======
        state['clauses'] = clauses
        self._step_5_print_stats(full_text, anchors, clauses)

        return state

    def _step_1_get_inputs(self, state: ParseGraphState) -> Tuple[str, List[Dict[str, Any]]]:
        """
        【步骤1】获取输入
        :param state: 项目状态字典（ParseGraphState）
        :return: (去噪后全文, DocFragment[] 列表)
        """
        full_text = state.get('full_text')
        if not full_text:
            raise StateFieldError(
                field_name='full_text',
                message='全文内容为空，请先执行 node_clean_text',
                expected_type=str
            )

        fragments = state.get('fragments')
        if not fragments:
            raise StateFieldError(
                field_name='fragments',
                message='解析块为空，请先执行 node_clean_text',
                expected_type=list
            )

        return full_text, fragments

    def _step_2_find_anchors(self, full_text: str) -> List[Dict[str, Any]]:
        """
        【步骤2】锚点扫描
        功能：用多个正则扫描全文，收集所有条款起点；
             去重同位置命中、剔除相互重叠的命中，保证区间切分不重叠
        :param full_text: 去噪后全文
        :return: 锚点列表 [{'start': int, 'end': int, 'text': str}]，按位置升序
        """
        candidates: List[Dict[str, Any]] = []
        for pattern in self._ANCHOR_PATTERNS:
            for match in pattern.finditer(full_text):
                candidates.append({
                    'start': match.start(),
                    'end': match.end(),
                    'text': match.group(),
                })

        # 1.按起点升序；同起点时优先保留更长的匹配（信息更全，如「第 1.1 条」）
        candidates.sort(key=lambda item: (item['start'], -len(item['text'])))

        # 2.去重同起点 + 剔除与上一条重叠的命中
        anchors: List[Dict[str, Any]] = []
        for candidate in candidates:
            if anchors and candidate['start'] < anchors[-1]['end']:
                continue  # 与前一条锚点重叠 → 丢弃
            if anchors and candidate['start'] == anchors[-1]['start']:
                continue  # 同起点 → 已保留更长的那条
            anchors.append(candidate)

        return anchors

    def _step_3_split_by_anchors(self, full_text: str,
                                 anchors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        【步骤3】按锚点切分区间
        功能：锚点之间的区间即为一个条款；锚点之前的文本作为「前言」块；
             无锚点时整篇作为一块（兜底，不阻塞）
        :param full_text: 去噪后全文
        :param anchors: 锚点列表
        :return: 区间列表 [{'start': int, 'end': int, 'anchor': str}]
        """
        # 1.兜底：一个锚点都没找到 → 整篇作为单块
        if not anchors:
            self.logger.warning(
                '未识别到任何条款锚点（第X条 / （一） / 1.1 / 1、），'
                '按兜底策略将整篇作为单个条款，后续可按段落粒度审查'
            )
            return [{'start': 0, 'end': len(full_text), 'anchor': ''}]

        segments: List[Dict[str, Any]] = []

        # 2.锚点之前的文本（合同标题、签约主体等）单独成块
        if anchors[0]['start'] > 0:
            segments.append({'start': 0, 'end': anchors[0]['start'], 'anchor': ''})

        # 3.逐个锚点切出区间，末尾锚点的区间延伸到全文结束
        for index, anchor in enumerate(anchors):
            segment_end = anchors[index + 1]['start'] if index + 1 < len(anchors) else len(full_text)
            segments.append({
                'start': anchor['start'],
                'end': segment_end,
                'anchor': anchor['text'],
            })

        return segments

    def _step_4_build_clauses(self, full_text: str, segments: List[Dict[str, Any]],
                              fragments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        【步骤4】类型归类 + 反查定位 + 组装 Clause
        :param full_text: 去噪后全文（切分原料）
        :param segments: 区间列表
        :param fragments: DocFragment[] 列表（提供 offset → 页码/段落反查）
        :return: Clause[] 列表
        """
        clauses: List[Dict[str, Any]] = []
        seq = 0

        for segment in segments:
            clause = self._build_clause(full_text, segment, fragments, seq)
            # 纯空白区间（锚点之间只有分隔符）直接丢弃
            if not clause['content']:
                continue
            clauses.append(clause)
            seq += 1

        return clauses

    def _build_clause(self, full_text: str, segment: Dict[str, Any],
                      fragments: List[Dict[str, Any]], seq: int) -> Dict[str, Any]:
        """
        【辅助函数】由区间组装单个 Clause
        功能：切出原文 → strip 并**同步修正偏移量** → 归类类型 → 反查页码段落
        :param full_text: 去噪后全文
        :param segment: 区间 {'start', 'end', 'anchor'}
        :param fragments: DocFragment[] 列表
        :param seq: 条款序号
        :return: Clause 字典
        """
        raw = full_text[segment['start']:segment['end']]

        # 1.strip 并同步修正偏移量，保证 full_text[start:end] == content 仍然成立
        lead_spaces = len(raw) - len(raw.lstrip())
        trail_spaces = len(raw) - len(raw.rstrip())
        content = raw.strip()
        start_offset = segment['start'] + lead_spaces
        end_offset = segment['end'] - trail_spaces

        # 2.条款类型归类
        clause_type, type_source = self._classify_clause_type(content)

        # 3.由偏移量反查页码与段落索引
        page_no, paragraph_index = self._locate(fragments, start_offset)

        return {
            'clause_id': f'cl_{seq + 1:03d}',
            'seq': seq,
            'clause_type': clause_type,
            'title': self._build_title(content),
            'content': content,
            'start_offset': start_offset,
            'end_offset': end_offset,
            'page_no': page_no,
            'paragraph_index': paragraph_index,
            # 归类依据（keyword / llm / fallback），便于排查误判；不入库
            'type_source': type_source,
        }

    def _build_title(self, content: str) -> str:
        """
        【辅助函数】从条款正文里提取标题
        功能：取正文开头到第一个标点/换行为止的部分，超过上限则截断
        :param content: 条款正文
        :return: 条款标题
        """
        first_line = content.split('\n', 1)[0]
        match = re.match(r'^(.{0,%d}?)[：:。；;]' % self._TITLE_MAX_CHARS, first_line)
        title = match.group(1) if match else first_line
        return title.strip()[:self._TITLE_MAX_CHARS]

    def _classify_clause_type(self, content: str) -> Tuple[str, str]:
        """
        【辅助函数】条款类型归类
        功能：先按关键词规则加权打分归类；未命中 → 走 LLM 辅助；仍拿不到 → 归 other
             打分规则：收录在标题区的关键词记 3 分，只出现在正文里的记 1 分
        :param content: 条款正文
        :return: (条款类型, 归类依据)
        """
        # 1.切出标题区：取正文开头到第一个标点/换行为止，最多 30 字符
        head = re.split(r'[：:。；;\n]', content, maxsplit=1)[0][:self._TITLE_REGION_CHARS]

        # 2.关键词规则：加权得分最高的类型胜出；同分时按关键词表顺序取靠前者
        best_type, best_score = '', 0
        for clause_type, keywords in self._TYPE_KEYWORDS:
            score = 0
            for keyword in keywords:
                if keyword in head:
                    score += self._TITLE_KEYWORD_WEIGHT
                elif keyword in content:
                    score += 1
            if score > best_score:
                best_type, best_score = clause_type, score

        if best_type:
            return best_type, 'keyword'

        # 3.规则归类不了 → 交给 LLM 辅助（P1；未配置/异常/mock/低置信度都归 None）
        llm_type = self._classify_by_llm(content)
        if llm_type:
            return llm_type, 'llm'

        # 4.兜底：归入 other（与 SPEC「置信度低则归入 other」一致）
        return self.DEFAULT_TYPE, 'fallback'

    def _classify_by_llm(self, content: str) -> Optional[str]:
        """
        LLM 辅助条款分类（SPEC 4.2.2 辅助策略 / SPEC 8.2 的 P1 提示词）

        调用 P1 返回 {clause_type, confidence}，置信度低于阈值返回 None（归 other）。
        **降级哲学与 OCR/审查一致**：LLM 未配置 / mock / 调用异常都只告警并返回 None，
        绝不阻塞解析主链路 —— 解析层的 LLM 是辅助，规则路径才是主线。

        :param content: 条款正文
        :return: 条款类型；未命中 / 置信度不足 / 不可用时返回 None
        """
        if not self.config.enable_llm_assist:
            return None

        from processor.parse_processor.prompt.parse_prompt import (
            build_p1_messages,
            parse_p1_response,
        )
        from utils.json_format_utils import safe_json_loads
        from utils.llm_utils import LLMProviderError

        try:
            provider = self._get_provider()
        except Exception as e:
            self.logger.warning(f'P1 条款分类：LLM Provider 不可用，跳过（{e}）')
            return None

        try:
            raw = provider.chat(build_p1_messages(content), json_mode=True)
        except LLMProviderError as e:
            self.logger.warning(f'P1 条款分类调用失败，跳过（{e}）')
            return None
        except Exception as e:  # 防御：Provider 实现可能抛非约定异常
            self.logger.warning(f'P1 条款分类调用异常，跳过（{e}）')
            return None

        result = parse_p1_response(safe_json_loads(raw, default={}))
        if result['confidence'] < self.config.llm_classify_min_confidence:
            self.logger.info(
                f'P1 条款分类置信度不足（{result["confidence"]:.2f} < '
                f'{self.config.llm_classify_min_confidence}），归入 other'
            )
            return None

        self.logger.info(
            f'P1 条款分类命中：{result["clause_type"]}（置信度 {result["confidence"]:.2f}）'
        )
        return result['clause_type']

    def _locate(self, fragments: List[Dict[str, Any]], offset: int) -> Tuple[int, int]:
        """
        【辅助函数】由全局字符偏移反查页码与段落索引
        功能：找到 offset 落在哪个块；若正好落在块之间的分隔符上，归到前一块
        :param fragments: DocFragment[] 列表
        :param offset: 全局字符偏移
        :return: (页码, 段落索引)
        """
        page_no, paragraph_index = 0, 0
        for fragment in fragments:
            if fragment.get('start_offset', 0) <= offset:
                page_no = fragment.get('page_no', 0)
                paragraph_index = fragment.get('paragraph_index', 0)
            else:
                break
        return page_no, paragraph_index

    def _step_5_print_stats(self, full_text: str, anchors: List[Dict[str, Any]],
                            clauses: List[Dict[str, Any]]) -> None:
        """
        【步骤5】输出统计日志
        """
        type_counter: Dict[str, int] = {}
        for clause in clauses:
            type_counter[clause['clause_type']] = type_counter.get(clause['clause_type'], 0) + 1

        self.log_step('全文长度', str(len(full_text)))
        self.log_step('锚点数', str(len(anchors)))
        self.log_step('条款数', str(len(clauses)))
        self.log_step('类型分布', json.dumps(type_counter, ensure_ascii=False))


if __name__ == '__main__':
    # 激活日志的全局配置
    setup_logging()

    from processor.parse_processor.config import ParseConfig
    from processor.parse_processor.nodes.node_clean_text import NodeCleanText

    node_clause_split = NodeClauseSplit()
    passed = 0

    def _prepare_state(raw_fragments: List[Dict[str, Any]]) -> ParseGraphState:
        """先跑一遍 node_clean_text 产出 full_text / 偏移量，再交给切分节点（顺便验证两节点可组合）"""
        clean_state = get_default_state()
        clean_state.update({'task_id': 'task-clause', 'fragments': raw_fragments})
        return NodeCleanText()(clean_state)

    def _assert_clause_offsets(clauses: List[Dict[str, Any]], full_text: str) -> None:
        """核心不变量：任意条款都能用全局偏移从 full_text 里原样切出来"""
        for clause in clauses:
            sliced = full_text[clause['start_offset']:clause['end_offset']]
            assert sliced == clause['content'], (
                f"条款偏移不一致！{clause['clause_id']}\n"
                f"  期望: {clause['content']!r}\n  实际: {sliced!r}"
            )

    # ---------- 一、标准合同：第X条锚点 ----------
    logging.getLogger().info('=' * 22 + ' 一、标准合同（第X条）' + '=' * 22)

    standard_fragments = [
        {'seq': 0, 'page_no': 1, 'paragraph_index': 0, 'kind': 'paragraph', 'bbox': None,
         'text': '软件采购合同'},
        {'seq': 1, 'page_no': 1, 'paragraph_index': 1, 'kind': 'paragraph', 'bbox': None,
         'text': '第一条 标的物：甲方向乙方采购企业管理系统软件一套。'},
        {'seq': 2, 'page_no': 1, 'paragraph_index': 2, 'kind': 'paragraph', 'bbox': None,
         'text': '第二条 付款方式：货到即付全款，未设验收条件。'},
        {'seq': 3, 'page_no': 2, 'paragraph_index': 3, 'kind': 'paragraph', 'bbox': None,
         'text': '第三条 知识产权：本合同软件的知识产权归乙方所有。'},
        {'seq': 4, 'page_no': 2, 'paragraph_index': 4, 'kind': 'paragraph', 'bbox': None,
         'text': '第四条 违约责任：甲方承担全部损失，乙方每日承担万分之一滞纳金。'},
        {'seq': 5, 'page_no': 3, 'paragraph_index': 5, 'kind': 'paragraph', 'bbox': None,
         'text': '第五条 争议解决：双方协商不成的，提交乙方所在地人民法院管辖。'},
    ]

    state = _prepare_state(standard_fragments)
    state['task_id'] = 'task-clause-standard'
    result = node_clause_split(state)
    clauses = result['clauses']
    full_text = result['full_text']

    # 断言 1：1 个前言块 + 5 个条款 = 6 块
    assert len(clauses) == 6, f'应有 6 个条款块（含前言），实际 {len(clauses)}'
    assert clauses[0]['title'] == '软件采购合同', '第一块应为锚点前的前言（合同标题）'
    assert clauses[0]['clause_type'] == 'other', '前言块无关键词命中，应为 other'

    # 断言 2：clause_id 与 seq 规范
    assert [c['clause_id'] for c in clauses] == [f'cl_{i + 1:03d}' for i in range(len(clauses))], \
        'clause_id 应为 cl_001 递增格式'
    assert [c['seq'] for c in clauses] == list(range(len(clauses))), 'seq 应连续递增'

    # 断言 3：类型归类正确
    type_by_seq = {c['seq']: c['clause_type'] for c in clauses}
    assert type_by_seq[1] == 'subject', '「标的物」应归为 subject'
    assert type_by_seq[2] == 'payment', '「付款方式」应归为 payment'
    assert type_by_seq[3] == 'ip', '「知识产权」应归为 ip'
    assert type_by_seq[4] == 'breach', '「违约责任」应归为 breach'
    assert type_by_seq[5] == 'jurisdiction', '「争议解决」应归为 jurisdiction'

    # 断言 4：归类依据为关键词
    assert all(c['type_source'] == 'keyword' for c in clauses[1:]), '五个条款都应命中关键词规则'

    # 断言 5：页码正确反查（跨页）
    assert clauses[3]['page_no'] == 2, '第三条在第 2 页'
    assert clauses[5]['page_no'] == 3, '第五条在第 3 页'
    assert clauses[1]['paragraph_index'] == 1, '第一条应定位到段落索引 1'

    # 断言 6【核心】：条款偏移与 full_text 完全一致
    _assert_clause_offsets(clauses, full_text)

    passed += 1
    logging.getLogger().info(
        f'  ✅ 切出 {len(clauses)} 块（含前言），类型/页码/偏移均正确')

    # ---------- 二、（一）/ 1.1 编号锚点 ----------
    logging.getLogger().info('=' * 22 + ' 二、其他编号形式' + '=' * 22)

    numbered_fragments = [
        {'seq': 0, 'page_no': 1, 'paragraph_index': 0, 'kind': 'paragraph', 'bbox': None,
         'text': '服务协议\n（一）服务内容：乙方提供运维服务。\n（二）服务费用：按月结算。'},
        {'seq': 1, 'page_no': 1, 'paragraph_index': 1, 'kind': 'paragraph', 'bbox': None,
         'text': '1.1 保密义务：双方对商业秘密承担保密义务，保密期限未约定。\n'
                 '1.2 数据安全：乙方应保障个人信息安全。'},
    ]
    state = _prepare_state(numbered_fragments)
    state['task_id'] = 'task-clause-numbered'
    result = node_clause_split(state)
    clauses = result['clauses']

    assert len(clauses) >= 5, f'应切出 1 前言 + 4 条，实际 {len(clauses)}'
    types = [c['clause_type'] for c in clauses]
    assert 'confidentiality' in types, '「保密义务」应被识别为 confidentiality'
    assert 'data_security' in types, '「数据安全」应被识别为 data_security'
    _assert_clause_offsets(clauses, result['full_text'])
    passed += 1
    logging.getLogger().info(f'  ✅ （一）/ 1.1 编号可切分，类型分布 {types}')

    # ---------- 三、兜底：无任何锚点 ----------
    logging.getLogger().info('=' * 22 + ' 三、兜底策略' + '=' * 22)

    no_anchor_fragments = [
        {'seq': 0, 'page_no': 1, 'paragraph_index': 0, 'kind': 'paragraph', 'bbox': None,
         'text': '本合同由甲乙双方友好协商订立，内容如下所述，双方均应严格遵守约定。'},
        {'seq': 1, 'page_no': 1, 'paragraph_index': 1, 'kind': 'paragraph', 'bbox': None,
         'text': '双方确认上述内容真实有效，特此声明。'},
    ]
    state = _prepare_state(no_anchor_fragments)
    state['task_id'] = 'task-clause-fallback'
    result = node_clause_split(state)
    clauses = result['clauses']

    assert len(clauses) == 1, f'无锚点时应兜底为单块，实际 {len(clauses)}'
    assert clauses[0]['clause_type'] == 'other', '兜底块应为 other'
    assert clauses[0]['type_source'] == 'fallback', '兜底块归类依据应为 fallback'
    assert '友好协商订立' in clauses[0]['content'], '兜底块应包含完整正文'
    _assert_clause_offsets(clauses, result['full_text'])
    passed += 1
    logging.getLogger().info('  ✅ 无锚点 → 兜底单块，不报错不阻塞')

    # ---------- 三点五、中文数字顿号锚点（真实语料教训）----------
    logging.getLogger().info('=' * 22 + ' 三点五、一、二、编号' + '=' * 22)

    cn_enum_fragments = [
        {'seq': 0, 'page_no': 1, 'paragraph_index': 0, 'kind': 'paragraph', 'bbox': None,
         'text': '销售合同'},
        {'seq': 1, 'page_no': 1, 'paragraph_index': 1, 'kind': 'paragraph', 'bbox': None,
         'text': '一、基本信息\n甲方（购买方）：某某公司\n乙方（销售方）：另一公司'},
        {'seq': 2, 'page_no': 1, 'paragraph_index': 2, 'kind': 'paragraph', 'bbox': None,
         'text': '二、货物名称及数量：5 台电脑'},
        {'seq': 3, 'page_no': 1, 'paragraph_index': 3, 'kind': 'paragraph', 'bbox': None,
         'text': '三、交付及验收\n1. 交付地点：深圳大道 10000 号\n2. 甲方在收到货物后的 7 天内进行验收。'},
        {'seq': 4, 'page_no': 1, 'paragraph_index': 4, 'kind': 'paragraph', 'bbox': None,
         'text': '四、其他\n双方协商解决争议。'},
    ]
    state = _prepare_state(cn_enum_fragments)
    state['task_id'] = 'task-clause-cn-enum'
    result = node_clause_split(state)
    clauses = result['clauses']

    assert len(clauses) >= 4, \
        f'「一、二、」编号应切成至少 4 条，实际 {len(clauses)}：{[c["title"] for c in clauses]}'
    titles = ''.join(c['title'] for c in clauses)
    assert '基本信息' in titles and '货物名称' in titles and '交付' in titles, \
        f'各编号条款应独立成条，实际标题：{titles}'
    _assert_clause_offsets(clauses, result['full_text'])
    passed += 1
    logging.getLogger().info(f'  ✅ 「一、二、三、」编号切成 {len(clauses)} 条，偏移自洽')

    # ---------- 四、P1 LLM 辅助条款分类（SPEC 8.2）----------
    logging.getLogger().info('=' * 22 + ' 四、P1 LLM 辅助分类 ' + '=' * 22)

    class _P1Stub:
        """P1 替身：固定返回一个分类结果"""

        is_mock = False
        name = 'p1-stub'

        def __init__(self, response: str):
            self._response = response
            self.call_count = 0

        def chat(self, messages, temperature=None, json_mode=False) -> str:
            self.call_count += 1
            return self._response

    # 4.1 高置信度 → 采纳 LLM 分类
    stub_node = NodeClauseSplit(provider=_P1Stub(
        '{"clause_type": "confidentiality", "title_candidate": "保密条款", "confidence": 0.93}'
    ))
    clause_type, source = stub_node._classify_clause_type('本段内容与双方合作过程中获悉的信息使用范围有关，任何一方不得擅自对外披露。')
    # 上面这段没有命中关键词表（"披露"不在 confidentiality 关键词里），落到 LLM
    assert (clause_type, source) == ('confidentiality', 'llm'), \
        f'高置信度应采纳 LLM 分类，实际 {(clause_type, source)}'
    passed += 1
    logging.getLogger().info('  ✅ P1 高置信度 → 采纳 LLM 分类（type_source=llm）')

    # 4.2 低置信度 → 归 other（SPEC 4.2.2）
    stub_node = NodeClauseSplit(provider=_P1Stub(
        '{"clause_type": "payment", "confidence": 0.31}'
    ))
    clause_type, source = stub_node._classify_clause_type('完全无法归类的奇怪段落。')
    assert (clause_type, source) == ('other', 'fallback'), \
        f'低置信度应归 other，实际 {(clause_type, source)}'
    passed += 1
    logging.getLogger().info('  ✅ P1 低置信度（< 0.6）→ 归入 other，不硬猜')

    # 4.3 LLM 异常 → 只告警归 other，不阻塞
    class _Boom:
        is_mock = False
        name = 'boom'

        def chat(self, messages, temperature=None, json_mode=False) -> str:
            raise RuntimeError('模拟 LLM 服务崩溃')

    stub_node = NodeClauseSplit(provider=_Boom())
    clause_type, source = stub_node._classify_clause_type('又一段无法归类的文本。')
    assert (clause_type, source) == ('other', 'fallback'), \
        f'LLM 异常应降级归 other，实际 {(clause_type, source)}'
    passed += 1
    logging.getLogger().info('  ✅ P1 调用异常 → 只告警归 other，解析主链路不受影响')

    # 4.4 关闭开关 → 完全不调用 LLM
    off_node = NodeClauseSplit(config=ParseConfig(enable_llm_assist=False),
                               provider=_P1Stub('{"clause_type": "payment", "confidence": 0.99}'))
    clause_type, source = off_node._classify_clause_type('无法归类的文本。')
    assert (clause_type, source) == ('other', 'fallback')
    assert off_node._provider_override.call_count == 0, '关闭开关后不应发起任何 LLM 调用'
    passed += 1
    logging.getLogger().info('  ✅ enable_llm_assist=false → 零调用，行为与接入前一致')

    # 4.5 mock 通道 → 不产出假分类（mock 响应没有 clause_type → 置信度 0 → other）
    from utils.llm_utils import MockLLMProvider
    mock_node = NodeClauseSplit(provider=MockLLMProvider())
    clause_type, source = mock_node._classify_clause_type('无法归类的文本。')
    assert (clause_type, source) == ('other', 'fallback'), \
        f'mock 通道不得编造分类，实际 {(clause_type, source)}'
    passed += 1
    logging.getLogger().info('  ✅ mock 通道 → 归 other（绝不产出假分类）')

    # ---------- 五、异常场景 ----------
    logging.getLogger().info('=' * 22 + ' 五、异常场景 ' + '=' * 22)

    err_cases = [
        ('全文为空', {}, StateFieldError),
        ('解析块为空', {'full_text': '第一条 标的物。'}, StateFieldError),
    ]


    for case_name, overrides, expected_exc in err_cases:
        init_state = get_default_state()
        init_state['task_id'] = 'task-clause-error'
        init_state.update(overrides)
        try:
            node_clause_split(init_state)
        except Exception as e:
            assert isinstance(e, ParseProcessError), f'{case_name} 应被基类包装成 ParseProcessError'
            assert isinstance(e.cause, expected_exc), \
                f'{case_name} 应抛出 {expected_exc.__name__}，实际为 {type(e.cause).__name__}'
            passed += 1
            logging.getLogger().info(f'  ✅ {case_name:<10} → 正确抛出 {expected_exc.__name__}')
        else:
            raise AssertionError(f'{case_name} 未抛出异常，用例失败')

    # ---------- 五、打印最终结果（便于人工核对）----------
    logging.getLogger().info('=' * 22 + ' 五、最终结果 ' + '=' * 22)
    logging.getLogger().info(json.dumps(result['clauses'], ensure_ascii=False, indent=4)[:1400])

    logging.getLogger().info(f'node_clause_split 自测全部通过 ✅ 共 {passed} 个用例')
