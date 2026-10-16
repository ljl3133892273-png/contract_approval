'''processor/parse_processor/nodes/node_meta_extract.py'''

'''
步骤分解
1. 接收状态: 取 `full_text` + `clauses`（clauses 用于把字段挂到所属条款上）。
2. 合同编号: 正则抽取「合同编号：XXX」。
3. 签约主体: 抽取甲方 / 乙方名称，并做邻近关联（见下）。
4. 统一社会信用代码: 18 位正则（GB 32100-2015），并与同段落的主体做邻近关联。
5. 合同金额 / 币种: 关键词定位 → 取窗口 → 先试中文大写、再试小写数字；币种在窗口内识别。
6. 履行期限: 定位「履行期限 / 合同期限 / 有效期」关键词，在窗口内抓两个日期并归一化为 YYYY-MM-DD。
7. 生效条件: 正则抓「本合同自……生效」。
8. 反查定位: 每个字段记录来源 Locator，供工作台「元数据高亮」使用。
9. 写回状态 + 统计日志。

需求分析
输入:
- `full_text`：去噪后全文
- `clauses`：Clause[]（用于把字段挂到所属条款）

输出:
- `meta`：ContractMeta 字典，含 SPEC 6.2 的全部字段 + locators + raw_json

设计要点（重要）:
    **元数据缺失绝不能报错**。SPEC 的 R-01 规则正是靠「未提取到相对方名称或信用代码」
    来判高风险的 —— 一旦抽不到就抛异常，R-01 永远不会触发，这条核心风控就废了。
    所以抽不到就如实留空（None / ''），并把「缺了哪些字段」记进日志与 meta。

方法论对应 SPEC 4.2.3:
- 主体 / 信用代码：正则 → 邻近文本关联 → P2 LLM 校验（见 `_normalize_by_llm`）
- 金额 / 币种：金额正则（大写 / 小写）→ P2 LLM 归一化（规则路径为主，LLM 只做校正）
- 履行期限 / 生效条件：规则路径（LLM 依存关系提取属生产增强，演示用规则兜底）

P2 降级哲学（与 P1 一致）:
    LLM 未配置 / mock / 异常 → 只告警并保留规则原值，绝不阻塞解析主链路。
'''

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from processor.parse_processor.base import BaseNode, setup_logging
from processor.parse_processor.exceptions import (
    ParseProcessError,
    StateFieldError,
)
from processor.parse_processor.state import ParseGraphState, get_default_state


# 中文数字 / 单位 / 节权（用于中文大写金额换算）
_CN_DIGITS = {
    '零': 0, '壹': 1, '贰': 2, '叁': 3, '肆': 4, '伍': 5, '陆': 6, '柒': 7, '捌': 8, '玖': 9,
    '一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '两': 2,
}
_CN_UNITS = {'十': 10, '拾': 10, '百': 100, '佰': 100, '千': 1000, '仟': 1000}
_CN_SECTIONS = {'万': 10000, '亿': 100000000}

# 币种识别关键词（顺序即优先级）
_CURRENCY_KEYWORDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ('USD', ('美元', 'USD', '美金')),
    ('HKD', ('港元', '港币', 'HKD')),
    ('EUR', ('欧元', 'EUR')),
    ('JPY', ('日元', 'JPY')),
    ('CNY', ('人民币', 'CNY', 'RMB', '￥', '¥')),
)


def chinese_amount_to_number(text: str) -> Optional[float]:
    """
    中文大写金额 → 数字。
    例：壹佰贰拾捌万元整 → 1280000.0 ；叁仟元伍角 → 3000.5

    :param text: 含中文数字与「元 / 角 / 分」的金额文本
    :return: 数值；无法换算时返回 None
    """
    if '元' not in text:
        return None

    integer_part, _, decimal_part = text.partition('元')
    integer_part = integer_part.strip()

    # ---------- 整数部分 ----------
    total, section, number = 0, 0, 0
    matched_digit = False

    for char in integer_part:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
            matched_digit = True
        elif char in _CN_UNITS:
            # 「十」这种省略前导一的写法：十 → 10
            section += (number or 1) * _CN_UNITS[char]
            number = 0
            matched_digit = True
        elif char in _CN_SECTIONS:
            section = (section + number) * _CN_SECTIONS[char]
            total += section
            section, number = 0, 0
            matched_digit = True
        # 其余字符（如「整」前的空格）忽略

    if not matched_digit:
        return None

    total += section + number

    # ---------- 小数部分（角 / 分）----------
    decimal = 0.0
    for char, weight in (('角', 0.1), ('分', 0.01)):
        index = decimal_part.find(char)
        if index > 0 and decimal_part[index - 1] in _CN_DIGITS:
            decimal += _CN_DIGITS[decimal_part[index - 1]] * weight

    return round(total + decimal, 2)


class NodeMetaExtract(BaseNode):
    '''元数据抽取节点：抽取合同核心元数据并保留来源 Locator'''
    name = 'node_meta_extract'

    # 关键词定位后向后取多少字符作为抽取窗口
    _WINDOW_CHARS = 60

    # 统一社会信用代码（GB 32100-2015）：共 18 位，不含容易混淆的 I / O / Z / S / V
    _CREDIT_CODE_PATTERN = re.compile(r'[0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10}')

    # 合同编号
    _CONTRACT_NO_PATTERN = re.compile(r'合同编号\s*[:：]\s*([A-Za-z0-9\-_/]{2,64})')

    # 签约主体（甲方 / 乙方）
    _PARTY_PATTERNS = {
        'party_a': re.compile(r'甲\s*方\s*(?:[（(][^）)]{0,10}[)）])?\s*[:：]\s*([^\n，。；;]{2,60})'),
        'party_b': re.compile(r'乙\s*方\s*(?:[（(][^）)]{0,10}[)）])?\s*[:：]\s*([^\n，。；;]{2,60})'),
    }

    # 金额 / 期限的关键词
    _AMOUNT_KEYWORDS = ('合同总金额', '合同金额', '总金额', '总价', '价款', '金额')
    _PERIOD_KEYWORDS = ('履行期限', '合同期限', '有效期', '服务期限')
    _EFFECTIVE_PATTERN = re.compile(r'本合同自([^\n。；;]{2,60}?)生效')
    _DATE_PATTERN = re.compile(r'(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*日?')
    _NUMBER_PATTERN = re.compile(r'\d[\d,]{2,19}(?:\.\d{1,2})?')

    def __init__(self, config=None, provider=None):
        """
        初始化节点
        :param config: 配置对象，默认使用全局配置
        :param provider: LLM Provider 覆盖项（仅测试注入用，P2 元数据归一化走它）
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
        节点：元数据抽取（node_meta_extract）
        整体流程：取输入 → 逐字段抽取 → 反查定位 → 组装 meta → 写回状态
        核心目的：抽出合同的「身份信息」，既供工作台展示高亮，
                 也为 R-01（主体缺失）等风险规则提供判定依据；
                 抽不到就如实留空，绝不因此报错
        必要参数：full_text
        更新参数：meta

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        # =======步骤一:获取输入=======
        full_text, clauses, fragments = self._step_1_get_inputs(state)

        # =======步骤二:逐个字段抽取=======
        meta: Dict[str, Any] = {}
        locators: Dict[str, Any] = {}

        # 合同编号
        value, locator = self._extract_contract_no(full_text, clauses, fragments)
        meta['contract_no'], locators['contract_no'] = value, locator

        # 统一社会信用代码
        credit_code, credit_locator = self._extract_credit_code(full_text, clauses, fragments)
        meta['credit_code'] = credit_code

        # 签约主体（甲方 / 乙方）
        for field_name, pattern in self._PARTY_PATTERNS.items():
            value, locator = self._extract_by_pattern(full_text, clauses, fragments, pattern, field_name)
            meta[field_name], locators[field_name] = value, locator

        # 信用代码归属：看它和哪一方出现在同一个段落
        if credit_code and credit_locator:
            credit_locator['owner'] = self._associate_credit_code(fragments, credit_locator)
        locators['credit_code'] = credit_locator

        # 合同金额 / 币种
        amount, amount_evidence, amount_offset = self._extract_amount(full_text)
        meta['amount'] = amount
        meta['currency'] = self._extract_currency(full_text, amount_offset)
        locators['amount'] = self._build_locator(
            full_text, clauses, fragments,
            amount_offset, amount_offset + len(amount_evidence), amount_evidence
        )

        # 履行期限
        period_start, period_end, period_evidence, period_offset = self._extract_period(full_text)
        meta['period_start'], meta['period_end'] = period_start, period_end
        locators['period'] = self._build_locator(
            full_text, clauses, fragments,
            period_offset, period_offset + len(period_evidence), period_evidence
        )

        # 生效条件
        value, locator = self._extract_by_pattern(
            full_text, clauses, fragments, self._EFFECTIVE_PATTERN, 'effective_condition'
        )
        meta['effective_condition'] = value
        locators['effective_condition'] = locator

        # =======步骤二点五:P2 LLM 校验/归一化（SPEC 8.2）=======
        # 只对规则已抽到的字段做归一化；取不到/异常时一律保留规则原值
        meta = self._step_2_5_normalize_by_llm(meta, full_text)

        # =======步骤三:汇总 locators 与 raw_json=======
        meta['locators'] = locators
        meta['raw_json'] = json.dumps(meta, ensure_ascii=False, default=str)

        # =======步骤四:写回状态 + 输出统计日志=======
        state['meta'] = meta
        self._step_print_stats(meta)

        return state

    def _step_1_get_inputs(self, state: ParseGraphState) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        【步骤1】获取输入
        :param state: 项目状态字典（ParseGraphState）
        :return: (去噪后全文, Clause[] 列表, DocFragment[] 列表)
        """
        full_text = state.get('full_text')
        if not full_text:
            raise StateFieldError(
                field_name='full_text',
                message='全文内容为空，请先执行 node_clean_text',
                expected_type=str
            )
        # clauses 可以为空（切分兜底失败也不该拦住元数据抽取）
        return full_text, state.get('clauses') or [], state.get('fragments') or []

    def _step_print_stats(self, meta: Dict[str, Any]) -> None:
        """
        【步骤4】输出统计日志
        功能：打印抽取到的字段，并**显式列出缺失字段**（R-01 高风险的判定依据）
        """
        # 参与「是否抽到」判定的核心字段
        core_fields = (
            'contract_no', 'party_a', 'party_b', 'credit_code',
            'amount', 'currency', 'period_start', 'period_end', 'effective_condition',
        )
        extracted = [name for name in core_fields if meta.get(name) not in (None, '', [])]
        missing = [name for name in core_fields if name not in extracted]

        self.log_step('已抽取字段', f'{len(extracted)}/{len(core_fields)} → {extracted}')
        if missing:
            # 缺失本身不是错误，但必须显式暴露：R-01 靠它判高风险
            self.log_step('缺失字段', str(missing))
        self.log_step('合同编号', str(meta.get('contract_no') or '-'))
        self.log_step('签约主体', f"甲方={meta.get('party_a') or '-'} / 乙方={meta.get('party_b') or '-'}")
        self.log_step('金额币种', f"{meta.get('amount')} {meta.get('currency') or '-'}")
        self.log_step(
            '履行期限',
            f"{meta.get('period_start') or '-'} ~ {meta.get('period_end') or '-'}"
        )

    # ==================== 各字段抽取 ====================

    def _extract_contract_no(self, full_text: str, clauses: List[Dict[str, Any]],
                             fragments: List[Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
        """
        【抽取】合同编号
        """
        return self._extract_by_pattern(
            full_text, clauses, fragments, self._CONTRACT_NO_PATTERN, 'contract_no'
        )

    def _extract_by_pattern(self, full_text: str, clauses: List[Dict[str, Any]],
                            fragments: List[Dict[str, Any]], pattern: "re.Pattern",
                            field_name: str) -> Tuple[str, Dict[str, Any]]:
        """
        【抽取】按正则抽第一个分组命中，并生成 Locator
        :param full_text: 全文
        :param clauses: Clause[] 列表
        :param fragments: DocFragment[] 列表（用于精确反查页码/段落）
        :param pattern: 带一个捕获组的正则
        :param field_name: 字段名（用于日志）
        :return: (字段值, Locator)；未命中返回 ('', {})
        """
        match = pattern.search(full_text)
        if not match:
            self.logger.info(f'[元数据] {field_name} 未抽取到')
            return '', {}

        value = match.group(1).strip()
        # Locator 记录「整段命中」的范围，便于前端高亮完整语义块
        locator = self._build_locator(
            full_text, clauses, fragments, match.start(), match.end(), match.group()
        )
        return value, locator

    def _extract_credit_code(self, full_text: str, clauses: List[Dict[str, Any]],
                             fragments: List[Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
        """
        【抽取】统一社会信用代码（18 位）
        """
        match = self._CREDIT_CODE_PATTERN.search(full_text)
        if not match:
            self.logger.info('[元数据] credit_code 未抽取到')
            return '', {}

        code = match.group()
        locator = self._build_locator(
            full_text, clauses, fragments, match.start(), match.end(), code
        )
        return code, locator

    def _associate_credit_code(self, fragments: List[Dict[str, Any]],
                               credit_locator: Dict[str, Any]) -> str:
        """
        【邻近关联】判断信用代码归属哪一方
        功能：SPEC 4.2.3 要求「正则 → 邻近文本关联」。这里取代码所在的**同一个段落**，
             看该段落里出现的是甲方还是乙方，据此把代码挂到对应主体上；
             两边都出现或都没出现则不强行归属，返回空字符串。
             注意：必须按段落判断，不能用「前后 N 字符」的窗口 ——
             合同头里甲方、乙方往往就挨着几行，窗口一放大会把两方都卷进来，永远判不出来。
        :param fragments: DocFragment[] 列表
        :param credit_locator: 信用代码的 Locator
        :return: 'party_a' / 'party_b' / ''
        """
        fragment = self._find_fragment(fragments, credit_locator['start_offset'])
        if not fragment:
            return ''

        paragraph_text = fragment.get('text', '')
        has_party_a = bool(re.search(r'甲\s*方', paragraph_text))
        has_party_b = bool(re.search(r'乙\s*方', paragraph_text))

        if has_party_a and not has_party_b:
            return 'party_a'
        if has_party_b and not has_party_a:
            return 'party_b'
        return ''

    def _extract_amount(self, full_text: str) -> Tuple[Optional[float], str, int]:
        """
        【抽取】合同金额
        功能：先定位金额关键词 → 向后取窗口 → 先试中文大写、再试小写数字
        :param full_text: 全文
        :return: (金额数值, 命中原文, 命中起始偏移)；未命中返回 (None, '', -1)
        """
        # 1.定位金额关键词（取最先出现的那个）
        keyword_index, keyword = -1, ''
        for candidate in self._AMOUNT_KEYWORDS:
            index = full_text.find(candidate)
            if index != -1 and (keyword_index == -1 or index < keyword_index):
                keyword_index, keyword = index, candidate

        if keyword_index == -1:
            self.logger.info('[元数据] amount 未抽取到（未找到金额关键词）')
            return None, '', -1

        # 2.关键词之后取窗口
        window_start = keyword_index + len(keyword)
        window_end = min(len(full_text), window_start + self._WINDOW_CHARS)
        window = full_text[window_start:window_end]

        # 3.先试中文大写金额（含「元」且含中文数字）
        for match in re.finditer(r'[零壹贰叁肆伍陆柒捌玖一二三四五六七八九十拾佰仟万亿两]{2,}元(?:[零壹贰叁肆伍陆柒捌玖一二三四五六七八九]角)?(?:[零壹贰叁肆伍陆柒捌玖一二三四五六七八九]分)?整?', window):
            number = chinese_amount_to_number(match.group())
            if number is not None:
                offset = window_start + match.start()
                self.log_step('金额识别', f'中文大写 → {number}')
                return number, match.group(), offset

        # 4.再试小写数字（至少 3 位，避免命中「第 2 条」这类小数字）
        # 真实语料教训：简式销售合同「货物价款……详见附件」的关键词窗口会罩住后面的
        # 地址门牌「深南大道10000号」—— 数字后紧跟「号」的是地址，跳过继续找下一个。
        for match in self._NUMBER_PATTERN.finditer(window):
            raw = match.group()
            after = window[match.end():match.end() + 2]
            if re.match(r'\s*号', after):
                self.logger.info(f'[元数据] amount 跳过疑似地址门牌号：{raw!r}（后跟"号"）')
                continue
            try:
                number = float(raw.replace(',', ''))
            except ValueError:
                continue
            offset = window_start + match.start()
            self.log_step('金额识别', f'小写数字 → {number}')
            return number, raw, offset

        self.logger.info('[元数据] amount 未抽取到（关键词后未见可用数字）')
        return None, '', -1

    def _extract_currency(self, full_text: str, amount_offset: int) -> str:
        """
        【抽取】币种
        功能：在金额所在的上下文窗口里识别币种；识别不到但出现「元」时按人民币处理
        :param full_text: 全文
        :param amount_offset: 金额命中偏移（-1 表示未抽到金额）
        :return: 币种代码（CNY / USD / HKD / EUR / JPY），识别不到返回 ''
        """
        if amount_offset < 0:
            return ''

        start = max(0, amount_offset - self._WINDOW_CHARS)
        end = min(len(full_text), amount_offset + self._WINDOW_CHARS)
        window = full_text[start:end]

        for currency, keywords in _CURRENCY_KEYWORDS:
            if any(keyword in window for keyword in keywords):
                return currency

        # 兜底：国内合同出现「元」且无其他币种 → 人民币
        if '元' in window:
            return 'CNY'
        return ''

    def _extract_period(self, full_text: str) -> Tuple[str, str, str, int]:
        """
        【抽取】履行期限
        功能：定位期限关键词 → 在窗口内抓两个日期 → 归一化为 YYYY-MM-DD 并保证先后有序
        :param full_text: 全文
        :return: (开始日期, 结束日期, 命中原文, 命中起始偏移)
        """
        keyword_index = -1
        for candidate in self._PERIOD_KEYWORDS:
            index = full_text.find(candidate)
            if index != -1 and (keyword_index == -1 or index < keyword_index):
                keyword_index = index

        if keyword_index == -1:
            self.logger.info('[元数据] period 未抽取到（未找到期限关键词）')
            return '', '', '', -1

        window_start = keyword_index
        window_end = min(len(full_text), keyword_index + self._WINDOW_CHARS * 2)
        window = full_text[window_start:window_end]

        # 抓日期并归一化
        dates = []
        for match in self._DATE_PATTERN.finditer(window):
            year, month, day = (int(part) for part in match.groups())
            if 1 <= month <= 12 and 1 <= day <= 31:
                dates.append(f'{year:04d}-{month:02d}-{day:02d}')

        if not dates:
            self.logger.info('[元数据] period 未抽取到（窗口内无有效日期）')
            return '', '', '', -1

        dates = sorted(set(dates))
        period_start = dates[0]
        period_end = dates[-1] if len(dates) > 1 else ''

        # 对窗口做 strip 时必须同步修正偏移量，否则 Locator 会与 evidence 对不上
        lead_spaces = len(window) - len(window.lstrip())
        evidence = window.strip()
        return period_start, period_end, evidence, window_start + lead_spaces

    # ==================== Locator ====================

    def _build_locator(self, full_text: str, clauses: List[Dict[str, Any]],
                       fragments: List[Dict[str, Any]], start_offset: int,
                       end_offset: int, evidence: str) -> Dict[str, Any]:
        """
        【辅助函数】构建来源 Locator
        功能：由全局偏移反查页码 / 段落索引 / 所属条款，供工作台「元数据高亮」使用。
             页码与段落必须反查 **fragments**（每块偏移精确），不能反查 clauses ——
             clause 只记录了自己的起始段落，长条款内部的字段会被定位到条款开头，位置是错的。
        :param full_text: 全文
        :param clauses: Clause[] 列表（用于反查所属条款）
        :param fragments: DocFragment[] 列表（用于精确反查页码/段落）
        :param start_offset: 命中起始偏移
        :param end_offset: 命中结束偏移
        :param evidence: 命中原文
        :return: Locator 字典；未命中时返回 {}
        """
        if start_offset < 0:
            return {}

        # 1.反查页码与段落（用碎片级偏移，精确）
        fragment = self._find_fragment(fragments, start_offset)
        page_no = fragment.get('page_no', 0) if fragment else 0
        paragraph_index = fragment.get('paragraph_index', 0) if fragment else 0

        # 2.反查所属条款
        clause_id = ''
        for clause in clauses:
            if clause.get('start_offset', 0) <= start_offset < clause.get('end_offset', 0):
                clause_id = clause.get('clause_id', '')
                break

        return {
            'clause_id': clause_id,
            'start_offset': start_offset,
            'end_offset': end_offset,
            'page_no': page_no,
            'paragraph_index': paragraph_index,
            'evidence': evidence.strip(),
        }

    def _find_fragment(self, fragments: List[Dict[str, Any]],
                       offset: int) -> Optional[Dict[str, Any]]:
        """
        【辅助函数】找出某个全局偏移落在哪个解析块里
        功能：取「起始偏移不超过该值」的最后一个块；正好落在块间分隔符上时归到前一块
        :param fragments: DocFragment[] 列表
        :param offset: 全局字符偏移
        :return: 命中的块；无命中返回 None
        """
        target = None
        for fragment in fragments:
            if fragment.get('start_offset', 0) <= offset:
                target = fragment
            else:
                break
        return target

    def _step_2_5_normalize_by_llm(self, meta: Dict[str, Any], full_text: str) -> Dict[str, Any]:
        """
        【步骤2.5】P2 LLM 校验 / 归一化（SPEC 4.2.3「LLM 校验/归一化」/ SPEC 8.2）

        只对**规则已经抽到**的字段做归一化（金额换算、信用代码大写、主体去修饰等）；
        模型拿不准 / 调用失败 / mock 通道 → 一律保留规则原值（返回 None 即不改写）。

        :param meta: 规则抽取的元数据（会被原地修正后返回）
        :param full_text: 全文（供 P2 上下文参考）
        :return: 归一化后的 meta
        """
        if not self.config.enable_llm_assist:
            return meta

        for field_name in ('contract_no', 'credit_code', 'party_a', 'party_b',
                           'amount', 'currency'):
            value = meta.get(field_name)
            if value is None or value == '':
                continue  # 没有候选值就没有可校验的对象（P2 是校验不是重抽）

            normalized = self._normalize_by_llm(field_name, {'value': value, 'source': 'regex'},
                                                full_text)
            if normalized is not None and normalized != value:
                self.logger.info(f'P2 归一化 {field_name}：{value!r} → {normalized!r}')
                meta[field_name] = normalized

        return meta

    def _normalize_by_llm(self, field_name: str, candidates: Dict[str, Any],
                          full_text: str = '') -> Optional[Any]:
        """
        单次 P2 调用（SPEC 4.2.3 中「LLM 校验」「LLM 归一化」）

        **降级哲学与 P1 一致**：LLM 未配置 / mock / 调用异常 / 返回非法
        都只告警并返回 None（调用方保留规则原值），绝不阻塞解析主链路。

        :param field_name: 字段名
        :param candidates: 规则抽到的候选值
        :param full_text: 全文（截取字段附近上下文供模型参考）
        :return: 归一化后的值；不可用时返回 None
        """
        from processor.parse_processor.prompt.parse_prompt import (
            build_p2_messages,
            parse_p2_response,
        )
        from utils.json_format_utils import safe_json_loads
        from utils.llm_utils import LLMProviderError

        try:
            provider = self._get_provider()
        except Exception as e:
            self.logger.warning(f'P2 元数据归一化：LLM Provider 不可用，跳过（{e}）')
            return None

        # 上下文：候选值在全文中的位置附近（找不到就给开头）
        context = full_text
        raw_value = str(candidates.get('value') or '')
        if raw_value:
            pos = full_text.find(raw_value)
            if pos >= 0:
                start = max(0, pos - 80)
                context = full_text[start:pos + len(raw_value) + 80]

        try:
            raw = provider.chat(build_p2_messages(field_name, candidates, context),
                                json_mode=True)
        except LLMProviderError as e:
            self.logger.warning(f'P2 元数据归一化调用失败（{field_name}），跳过（{e}）')
            return None
        except Exception as e:  # 防御：Provider 实现可能抛非约定异常
            self.logger.warning(f'P2 元数据归一化调用异常（{field_name}），跳过（{e}）')
            return None

        return parse_p2_response(safe_json_loads(raw, default={}), field_name)


if __name__ == '__main__':
    # 激活日志的全局配置
    setup_logging()

    from processor.parse_processor.nodes.node_clean_text import NodeCleanText
    from processor.parse_processor.nodes.node_clause_split import NodeClauseSplit

    node_meta_extract = NodeMetaExtract()
    passed = 0

    def _prepare_state(raw_fragments: List[Dict[str, Any]]) -> ParseGraphState:
        """先跑 clean_text → clause_split，再交给元数据抽取（验证三节点可组合）"""
        state = get_default_state()
        state.update({'task_id': 'task-meta', 'fragments': raw_fragments})
        state = NodeCleanText()(state)
        return NodeClauseSplit()(state)

    def _assert_locator_offsets(meta: Dict[str, Any], full_text: str) -> None:
        """Locator 不变量：locator 记录的偏移能从 full_text 里切出对应证据"""
        for field_name, locator in meta['locators'].items():
            if not locator:
                continue
            sliced = full_text[locator['start_offset']:locator['end_offset']]
            assert sliced == locator['evidence'], (
                f'Locator 偏移不一致！{field_name}\n'
                f"  期望: {locator['evidence']!r}\n  实际: {sliced!r}"
            )

    # ---------- 一、中文大写金额换算（纯函数）----------
    logging.getLogger().info('=' * 20 + ' 一、中文大写金额换算 ' + '=' * 20)

    amount_cases = [
        ('壹佰贰拾捌万元整', 1280000.0),
        ('叁仟元整', 3000.0),
        ('伍拾万元', 500000.0),
        ('壹万元伍角', 10000.5),
        ('拾万元', 100000.0),
        ('玖仟捌佰柒拾陆元', 9876.0),
        ('壹亿元', 100000000.0),
    ]
    for text, expected in amount_cases:
        actual = chinese_amount_to_number(text)
        assert actual == expected, f'{text} 应换算为 {expected}，实际 {actual}'
        passed += 1
    logging.getLogger().info(f'  ✅ {len(amount_cases)} 个中文大写金额换算正确')

    # ---------- 二、标准合同：全字段抽取 ----------
    logging.getLogger().info('=' * 20 + ' 二、标准合同全字段 ' + '=' * 20)

    standard_fragments = [
        {'seq': 0, 'page_no': 1, 'paragraph_index': 0, 'kind': 'paragraph', 'bbox': None,
         'text': '软件采购合同\n合同编号：CG-2026-0918-001'},
        {'seq': 1, 'page_no': 1, 'paragraph_index': 1, 'kind': 'paragraph', 'bbox': None,
         'text': '甲方：某某科技有限公司\n统一社会信用代码：91310000MA1FL1234X'},
        {'seq': 2, 'page_no': 1, 'paragraph_index': 2, 'kind': 'paragraph', 'bbox': None,
         'text': '乙方：某某软件股份有限公司'},
        {'seq': 3, 'page_no': 1, 'paragraph_index': 3, 'kind': 'paragraph', 'bbox': None,
         'text': '第一条 合同金额：人民币 1,280,000.00 元（大写：壹佰贰拾捌万元整）。'},
        {'seq': 4, 'page_no': 2, 'paragraph_index': 4, 'kind': 'paragraph', 'bbox': None,
         'text': '第二条 履行期限：自2026年1月1日起至2026年12月31日止。'},
        {'seq': 5, 'page_no': 2, 'paragraph_index': 5, 'kind': 'paragraph', 'bbox': None,
         'text': '第三条 生效条件：本合同自双方签字盖章之日起生效。'},
    ]

    state = _prepare_state(standard_fragments)
    state['task_id'] = 'task-meta-standard'
    result = node_meta_extract(state)
    meta = result['meta']
    full_text = result['full_text']

    assert meta['contract_no'] == 'CG-2026-0918-001', f"合同编号错误：{meta['contract_no']}"
    assert meta['party_a'] == '某某科技有限公司', f"甲方错误：{meta['party_a']}"
    assert meta['party_b'] == '某某软件股份有限公司', f"乙方错误：{meta['party_b']}"
    assert meta['credit_code'] == '91310000MA1FL1234X', f"信用代码错误：{meta['credit_code']}"
    assert meta['amount'] == 1280000.00, f"金额错误：{meta['amount']}"
    assert meta['currency'] == 'CNY', f"币种错误：{meta['currency']}"
    assert meta['period_start'] == '2026-01-01', f"期限起错误：{meta['period_start']}"
    assert meta['period_end'] == '2026-12-31', f"期限止错误：{meta['period_end']}"
    # 正则捕获的是「本合同自」与「生效」之间的部分，即「双方签字盖章之日起」
    assert meta['effective_condition'] == '双方签字盖章之日起', \
        f"生效条件错误：{meta['effective_condition']}"

    # 断言：信用代码邻近关联到甲方
    assert meta['locators']['credit_code']['owner'] == 'party_a', \
        '信用代码出现在甲方段落，应关联到 party_a'

    # 断言：字段挂到了正确条款上
    assert meta['locators']['amount']['clause_id'], '金额应能挂到所属条款'
    assert meta['locators']['period']['page_no'] == 2, '履行期限在第 2 页'

    # 断言【核心】：Locator 偏移与 full_text 一致
    _assert_locator_offsets(meta, full_text)

    # 断言：raw_json 可解析回字典
    assert json.loads(meta['raw_json'])['party_a'] == meta['party_a'], 'raw_json 应可还原'

    passed += 1
    logging.getLogger().info('  ✅ 标准合同 9 个字段全抽到，Locator 偏移自洽')

    # ---------- 三、金额只看小写 / 外币 ----------
    logging.getLogger().info('=' * 20 + ' 三、小写金额与外币 ' + '=' * 20)

    usd_fragments = [
        {'seq': 0, 'page_no': 1, 'paragraph_index': 0, 'kind': 'paragraph', 'bbox': None,
         'text': '技术许可协议'},
        {'seq': 1, 'page_no': 1, 'paragraph_index': 1, 'kind': 'paragraph', 'bbox': None,
         'text': '第一条 合同金额：美元 250,000.00 USD。'},
    ]
    state = _prepare_state(usd_fragments)
    state['task_id'] = 'task-meta-usd'
    result = node_meta_extract(state)
    meta = result['meta']

    assert meta['amount'] == 250000.00, f'小写金额抽取错误：{meta["amount"]}'
    assert meta['currency'] == 'USD', f'外币识别错误：{meta["currency"]}'
    _assert_locator_offsets(meta, result['full_text'])
    passed += 1
    logging.getLogger().info(f'  ✅ 小写金额 {meta["amount"]} / 币种 {meta["currency"]}')

    # 真实语料教训：地址门牌号不应被抽成金额（「价款详见附件」窗口罩住「深南大道10000号」）
    addr_fragments = [
        {'seq': 0, 'page_no': 1, 'paragraph_index': 0, 'kind': 'paragraph', 'bbox': None,
         'text': '销售合同'},
        {'seq': 1, 'page_no': 1, 'paragraph_index': 1, 'kind': 'paragraph', 'bbox': None,
         'text': '一、基本信息'},
        {'seq': 2, 'page_no': 1, 'paragraph_index': 2, 'kind': 'paragraph', 'bbox': None,
         'text': '二、货物名称及数量：5 台电脑。货物价款及支付条款，详见附件。'},
        {'seq': 3, 'page_no': 1, 'paragraph_index': 3, 'kind': 'paragraph', 'bbox': None,
         'text': '三、交付及验收：货物交付地点：深南大道10000号'},
    ]
    state = _prepare_state(addr_fragments)
    state['task_id'] = 'task-meta-addr'
    meta = node_meta_extract(state)['meta']
    assert meta['amount'] is None,         f'地址门牌号 10000 不应被抽成金额，实际 {meta["amount"]}'
    passed += 1
    logging.getLogger().info('  ✅ 地址门牌号（深南大道10000号）不被误抽为金额')

    # ---------- 三点五、P2 LLM 归一化（SPEC 8.2）----------
    logging.getLogger().info('=' * 20 + ' 三点五、P2 LLM 归一化 ' + '=' * 20)

    class _P2Stub:
        """P2 替身：按字段返回固定归一化结果"""

        is_mock = False
        name = 'p2-stub'

        def __init__(self, responses: Dict[str, str]):
            self._responses = responses
            self.calls: List[str] = []

        def chat(self, messages, temperature=None, json_mode=False) -> str:
            # 从 user 消息里抠字段名，返回对应响应
            import json as _json
            for field in self._responses:
                if f'"field": "{field}"' in messages[1]['content']:
                    self.calls.append(field)
                    return self._responses[field]
            return '{}'

    # 1.信用代码小写 → P2 归一化为大写
    stub = _P2Stub({
        'credit_code': '{"credit_code": "91310000MA1FL5678Y"}',
        'amount': '{"amount": 1280000}',
        'party_a': '{"party_a": "某某科技有限公司"}',
    })
    p2_node = NodeMetaExtract(provider=stub)
    lower_code_fragments = [
        {'seq': 0, 'page_no': 1, 'paragraph_index': 0, 'kind': 'paragraph', 'bbox': None,
         'text': '软件采购合同'},
        {'seq': 1, 'page_no': 1, 'paragraph_index': 1, 'kind': 'paragraph', 'bbox': None,
         'text': '甲方：某某科技有限公司'},
        {'seq': 2, 'page_no': 1, 'paragraph_index': 2, 'kind': 'paragraph', 'bbox': None,
         'text': '第一条 合同金额：人民币 1,280,000.00 元。'},
    ]
    state = _prepare_state(lower_code_fragments)
    state['task_id'] = 'task-meta-p2'
    meta = p2_node(state)['meta']
    # 规则已抽到的字段被 P2 校验（本例规则值本来就正确，P2 返回一致 → 不改写）
    assert meta['amount'] == 1280000.0, f'金额应保持规则值，实际 {meta["amount"]}'
    assert 'amount' in stub.calls and 'party_a' in stub.calls, '非空字段应过 P2'
    passed += 1
    logging.getLogger().info('  ✅ P2 对非空字段逐一校验；规则值正确时原样保留')

    # 2.P2 返回归一化值 → 改写
    stub2 = _P2Stub({'party_a': '{"party_a": "归一化后的甲方法定全称有限公司"}'})
    p2_node2 = NodeMetaExtract(provider=stub2)
    state2 = _prepare_state(lower_code_fragments)
    state2['task_id'] = 'task-meta-p2-rewrite'
    meta2 = p2_node2(state2)['meta']
    assert meta2['party_a'] == '归一化后的甲方法定全称有限公司', \
        f'P2 归一化应改写字段，实际 {meta2["party_a"]}'
    passed += 1
    logging.getLogger().info('  ✅ P2 返回归一化值 → 改写字段（日志留痕）')

    # 3.P2 异常 → 保留规则原值，不阻塞
    class _Boom2:
        is_mock = False
        name = 'boom'

        def chat(self, messages, temperature=None, json_mode=False) -> str:
            raise RuntimeError('模拟 LLM 服务崩溃')

    p2_node3 = NodeMetaExtract(provider=_Boom2())
    state3 = _prepare_state(lower_code_fragments)
    state3['task_id'] = 'task-meta-p2-boom'
    meta3 = p2_node3(state3)['meta']
    assert meta3['party_a'] == '某某科技有限公司', 'P2 异常时应保留规则原值'
    assert meta3['amount'] == 1280000.0
    passed += 1
    logging.getLogger().info('  ✅ P2 调用异常 → 保留规则原值，解析不阻塞')

    # 4.关闭开关 → 零调用
    from processor.parse_processor.config import ParseConfig as _ParseCfg
    stub4 = _P2Stub({'party_a': '{"party_a": "不应出现"}'})
    p2_node4 = NodeMetaExtract(config=_ParseCfg(enable_llm_assist=False), provider=stub4)
    state4 = _prepare_state(lower_code_fragments)
    state4['task_id'] = 'task-meta-p2-off'
    meta4 = p2_node4(state4)['meta']
    assert meta4['party_a'] == '某某科技有限公司', '关闭开关后不应改写'
    assert stub4.calls == [], '关闭开关后不应发起任何 P2 调用'
    passed += 1
    logging.getLogger().info('  ✅ enable_llm_assist=false → 零调用，规则值原样保留')

    # 5.mock 通道 → 不产出假归一化（mock 响应取不到字段 → 返回 None → 保留原值）
    from utils.llm_utils import MockLLMProvider
    p2_node5 = NodeMetaExtract(provider=MockLLMProvider())
    state5 = _prepare_state(lower_code_fragments)
    state5['task_id'] = 'task-meta-p2-mock'
    meta5 = p2_node5(state5)['meta']
    assert meta5['party_a'] == '某某科技有限公司', 'mock 通道不得编造归一化结果'
    passed += 1
    logging.getLogger().info('  ✅ mock 通道 → 保留规则原值（绝不产出假归一化）')

    # ---------- 四、字段缺失：不得报错 ----------
    logging.getLogger().info('=' * 20 + ' 四、字段缺失（R-01 判定依据）' + '=' * 20)

    missing_fragments = [
        {'seq': 0, 'page_no': 1, 'paragraph_index': 0, 'kind': 'paragraph', 'bbox': None,
         'text': '本合同由双方友好协商订立，具体条款如下所述内容。'},
        {'seq': 1, 'page_no': 1, 'paragraph_index': 1, 'kind': 'paragraph', 'bbox': None,
         'text': '双方均应严格遵守上述各项约定，特此声明确认。'},
    ]
    state = _prepare_state(missing_fragments)
    state['task_id'] = 'task-meta-missing'
    result = node_meta_extract(state)
    meta = result['meta']

    # 关键：缺失绝不能抛异常，否则 R-01（主体缺失 → 高风险）就永远不会触发
    assert meta['party_b'] == '', '抽不到的字段应留空字符串'
    assert meta['amount'] is None, '抽不到的金额应为 None'
    assert meta['credit_code'] == '', '抽不到的信用代码应留空'
    assert meta['period_start'] == '' and meta['period_end'] == '', '抽不到的期限应留空'
    passed += 1
    logging.getLogger().info('  ✅ 字段全缺失 → 如实留空且不报错（R-01 可正常判定）')

    # ---------- 五、异常场景 ----------
    logging.getLogger().info('=' * 20 + ' 五、异常场景 ' + '=' * 20)

    init_state = get_default_state()
    init_state['task_id'] = 'task-meta-error'
    try:
        node_meta_extract(init_state)
    except Exception as e:
        assert isinstance(e, ParseProcessError), '应被基类包装成 ParseProcessError'
        assert isinstance(e.cause, StateFieldError), \
            f'应抛 StateFieldError，实际 {type(e.cause).__name__}'
        passed += 1
        logging.getLogger().info('  ✅ 全文为空 → 正确抛出 StateFieldError')
    else:
        raise AssertionError('全文为空未抛出异常，用例失败')

    # ---------- 六、打印最终结果（便于人工核对）----------
    logging.getLogger().info('=' * 20 + ' 六、最终结果 ' + '=' * 20)
    logging.getLogger().info(json.dumps(
        {key: value for key, value in result['meta'].items() if key != 'raw_json'},
        ensure_ascii=False, indent=4, default=str
    ))

    logging.getLogger().info(f'node_meta_extract 自测全部通过 ✅ 共 {passed} 个用例')
