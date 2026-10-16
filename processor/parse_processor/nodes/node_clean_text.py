'''processor/parse_processor/nodes/node_clean_text.py'''

'''
步骤分解
1. 接收状态: 取 `fragments`（node_parse_docx / node_parse_pdf / node_ocr 的产出）。
2. 逐块文本规范: 统一换行符、去每行首尾空白、压缩连续空行。
3. 去页眉页脚: 短文本块在多个不同页重复出现 → 判为页眉/页脚删除（仅当有真实页码时生效）。
4. 去编号噪音: 删除「第N页」「- N -」「N / M」以及纯数字行（可配置）。
5. 重建全局偏移基准: 生成 `full_text`，并为每块写入 start_offset / end_offset。
6. 正文为空判定 + 写回状态 + 统计日志。

需求分析
输入:
- `fragments`：DocFragment[]

输出:
- `fragments`：清洗后的 DocFragment[]（新增 start_offset / end_offset）
- `full_text`：去噪后全文

为什么这一步很关键（Locator 的基准点）:
    SPEC 4.2.4 规定 Locator 的偏移量基于「整篇去噪后文本的全局字符偏移」。
    本节点就是产出这份基准文本的地方：full_text = 各块文本用 "\\n" 拼接。
    因此对任意块都恒有  full_text[start_offset:end_offset] == 该块 text，
    后续条款切分只要在 full_text 上找位置，就能反推出任意条款的页码与段落。
    这个等式在本文件的自测里被逐块断言，破坏它等于破坏整个高亮联动。
'''

import json
import logging
import re
from typing import Any, Dict, List

from processor.parse_processor.base import BaseNode, setup_logging
from processor.parse_processor.exceptions import (
    ParseProcessError,
    StateFieldError,
    ValidationError,
)
from processor.parse_processor.state import ParseGraphState, get_default_state


class NodeCleanText(BaseNode):
    '''文本预处理节点：去噪 + 建立全局字符偏移基准'''
    name = 'node_clean_text'

    # 块的连接符：决定了全局偏移的算法，不要随意改动
    _SEPARATOR = '\n'

    # 编号噪音正则（整行匹配）
    _NOISE_PATTERNS = (
        re.compile(r'^第\s*\d+\s*页\s*(共\s*\d+\s*页)?$'),  # 第 3 页 / 第 3 页 共 12 页
        re.compile(r'^第\s*\d+\s*页\s*/\s*共?\s*\d+\s*页$'),  # 第 3 页 / 共 12 页
        re.compile(r'^-\s*\d+\s*-$'),  # - 3 -
        re.compile(r'^\d+\s*/\s*\d+$'),  # 3 / 12
    )
    # 纯数字行（页码噪音）
    _PURE_NUMBER_PATTERN = re.compile(r'^\d{1,4}$')

    def process(self, state: ParseGraphState) -> ParseGraphState:
        """
        节点：文本预处理（node_clean_text）
        整体流程：取块 → 文本规范 → 去页眉页脚 → 去编号噪音 → 重建全局偏移 → 写回状态
        核心目的：把解析出来的原始块清洗成干净正文，并建立 Locator 的全局字符偏移基准
        必要参数：fragments
        更新参数：fragments、full_text

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        # =======步骤一:获取输入块=======
        fragments = self._step_1_get_inputs(state)

        # =======步骤二:逐块文本规范=======
        # 作用：统一换行符、去每行首尾空白、压缩连续空行
        normalized = self._step_2_normalize(fragments)

        # =======步骤三:去页眉页脚=======
        # 作用：短文本块在多个不同页重复出现 → 判为页眉/页脚删除
        without_header_footer, dropped_hf = self._step_3_drop_header_footer(normalized)

        # =======步骤四:去编号噪音=======
        # 作用：删除页码行、纯数字行等噪音
        cleaned, dropped_noise = self._step_4_drop_noise(without_header_footer)

        # =======步骤五:正文为空判定=======
        self._step_5_check_content(cleaned)

        # =======步骤六:重建全局偏移基准=======
        # 作用：生成 full_text 并给每块写入 start_offset / end_offset
        full_text = self._step_6_rebuild_offsets(cleaned)

        # =======步骤七:写回状态 + 输出统计日志=======
        state['fragments'] = cleaned
        state['full_text'] = full_text
        self._step_7_print_stats(fragments, cleaned, full_text, dropped_hf, dropped_noise)

        return state

    def _step_1_get_inputs(self, state: ParseGraphState) -> List[Dict[str, Any]]:
        """
        【步骤1】获取输入块
        :param state: 项目状态字典（ParseGraphState）
        :return: DocFragment[] 列表
        """
        fragments = state.get('fragments')
        if not fragments:
            raise StateFieldError(
                field_name='fragments',
                message='待清洗的解析块为空，请先执行解析节点',
                expected_type=list
            )
        return fragments

    def _step_2_normalize(self, fragments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        【步骤2】逐块文本规范
        功能：统一换行符 → 去每行首尾空白 → 压缩连续空行（3 行以上压成 1 行空行）
        :param fragments: 原始块列表
        :return: 规范化后的块列表（保留原块的其他字段）
        """
        normalized = []
        for fragment in fragments:
            text = fragment.get('text', '')
            # 1.统一换行符，消除不同系统的差异
            text = text.replace('\r\n', '\n').replace('\r', '\n')
            # 2.逐行去首尾空白
            lines = [line.strip() for line in text.split('\n')]
            # 3.压缩连续空行：连续 2 个以上空行只保留 1 个
            compressed: List[str] = []
            for line in lines:
                if not line and compressed and not compressed[-1]:
                    continue
                compressed.append(line)

            new_fragment = dict(fragment)
            new_fragment['text'] = '\n'.join(compressed).strip()
            normalized.append(new_fragment)

        return normalized

    def _step_3_drop_header_footer(self, fragments: List[Dict[str, Any]]) -> tuple:
        """
        【步骤3】去页眉页脚
        功能：短文本块若在「多个不同页」重复出现，判为页眉/页脚删除。
             没有真实页码信息时（DOCX 来源 page_no 全为 0）本步骤不生效。
        :param fragments: 规范化后的块列表
        :return: (清洗后的块列表, 删除的块数)
        """
        # 1.统计每个「去数字后」的文本出现在哪些页
        pages_of_text: Dict[str, set] = {}
        for fragment in fragments:
            key = self._header_footer_key(fragment.get('text', ''))
            if not key:
                continue
            pages_of_text.setdefault(key, set()).add(fragment.get('page_no', 0))

        # 2.只有跨页（>= 2 个不同页）且页码真实有效时才认定
        repeated_keys = {
            key for key, pages in pages_of_text.items()
            if len(pages - {0}) >= 2
        }

        kept, dropped = [], 0
        for fragment in fragments:
            key = self._header_footer_key(fragment.get('text', ''))
            if key and key in repeated_keys:
                dropped += 1
                continue
            kept.append(fragment)

        # 安全保护：页眉页脚不可能构成整篇正文。
        # 如果这一步把全部块都判成页眉页脚，说明判定规则被"骗"了
        # （典型场景：每页正文只有一条只差页码的短文本），
        # 此时放弃删除，保留原块，绝不能把整篇清成空。
        if not kept:
            self.logger.warning(
                f'页眉页脚判定将删除全部 {len(fragments)} 块 —— 这不合理，已放弃本次删除。'
            )
            return fragments, 0

        if dropped:
            self.log_step('去页眉页脚', f'删除 {dropped} 块（跨页重复）')
        return kept, dropped

    def _header_footer_key(self, text: str) -> str:
        """
        【辅助函数】生成页眉页脚的比对键
        功能：只对「短文本」做跨页重复判定，并把数字抹掉（页码不同但页眉相同）
        :param text: 块文本
        :return: 比对键；不适合做页眉页脚判定时返回空字符串
        """
        stripped = text.strip()
        if not stripped or len(stripped) > self.config.clean_repeat_max_chars:
            return ''
        # 把数字统一抹掉，这样「XX公司 第 1 页」与「XX公司 第 2 页」会归为同一个键
        return re.sub(r'\d+', '#', stripped)

    def _step_4_drop_noise(self, fragments: List[Dict[str, Any]]) -> tuple:
        """
        【步骤4】去编号噪音
        功能：删除页码行与纯数字行；其余行原样保留
        :param fragments: 块列表
        :return: (清洗后的块列表, 删除的行数)
        """
        cleaned, dropped_lines = [], 0

        for fragment in fragments:
            kept_lines = []
            for line in fragment.get('text', '').split('\n'):
                stripped = line.strip()
                if not stripped:
                    kept_lines.append(line)
                    continue

                # 页码噪音
                if any(pattern.match(stripped) for pattern in self._NOISE_PATTERNS):
                    dropped_lines += 1
                    continue
                # 纯数字行（可配置关闭）
                if (self.config.clean_drop_pure_number_lines
                        and self._PURE_NUMBER_PATTERN.match(stripped)):
                    dropped_lines += 1
                    continue

                kept_lines.append(line)

            new_text = '\n'.join(kept_lines).strip()
            if not new_text:
                continue  # 整块都是噪音 → 丢弃该块

            new_fragment = dict(fragment)
            new_fragment['text'] = new_text
            cleaned.append(new_fragment)

        if dropped_lines:
            self.log_step('去编号噪音', f'删除 {dropped_lines} 行')
        return cleaned, dropped_lines

    def _step_5_check_content(self, cleaned: List[Dict[str, Any]]) -> None:
        """
        【步骤5】正文为空判定
        功能：清洗后已无任何有效正文 → 报错（对应 SPEC 11.1「正文为空」）
        :param cleaned: 清洗后的块列表
        """
        full_text = ''.join(fragment.get('text', '') for fragment in cleaned)
        if not full_text.strip():
            raise ValidationError(message='文本清洗后正文为空，无有效内容可审查')

    def _step_6_rebuild_offsets(self, cleaned: List[Dict[str, Any]]) -> str:
        """
        【步骤6】重建全局偏移基准
        功能：用连接符拼接全文，并逐块写入 start_offset / end_offset。
             这是 Locator 的坐标原点，务必保证 full_text[start:end] == 块 text。
        :param cleaned: 清洗后的块列表
        :return: 去噪后全文
        """
        cursor = 0
        parts: List[str] = []

        for index, fragment in enumerate(cleaned):
            text = fragment['text']
            fragment['start_offset'] = cursor
            fragment['end_offset'] = cursor + len(text)
            cursor = fragment['end_offset'] + len(self._SEPARATOR)
            fragment['seq'] = index  # 重新编号，保证 seq 连续
            parts.append(text)

        return self._SEPARATOR.join(parts)

    def _step_7_print_stats(self, original: List[Dict[str, Any]], cleaned: List[Dict[str, Any]],
                            full_text: str, dropped_hf: int, dropped_noise: int) -> None:
        """
        【步骤7】输出统计日志
        """
        self.log_step('原始块数', str(len(original)))
        self.log_step('清洗后块数', str(len(cleaned)))
        self.log_step('删除块/行', f'页眉页脚 {dropped_hf} 块 / 噪音 {dropped_noise} 行')
        self.log_step('全文字符数', str(len(full_text)))


if __name__ == '__main__':
    # 激活日志的全局配置
    setup_logging()

    from processor.parse_processor.config import ParseConfig

    node_clean_text = NodeCleanText()
    passed = 0

    def _assert_offsets_consistent(fragments: List[Dict[str, Any]], full_text: str) -> None:
        """核心不变量：任意块都能用全局偏移从 full_text 里原样切出来"""
        for fragment in fragments:
            sliced = full_text[fragment['start_offset']:fragment['end_offset']]
            assert sliced == fragment['text'], (
                f'偏移不一致！seq={fragment["seq"]}\n'
                f'  期望: {fragment["text"]!r}\n  实际: {sliced!r}'
            )

    # ---------- 一、正常场景：页眉页脚 + 页码 + 空行 ----------
    logging.getLogger().info('=' * 22 + ' 一、正常场景 ' + '=' * 22)

    raw_fragments = [
        {'seq': 0, 'page_no': 1, 'paragraph_index': 0, 'kind': 'paragraph',
         'text': '某某科技有限公司 合同专用', 'bbox': None},
        {'seq': 1, 'page_no': 1, 'paragraph_index': 1, 'kind': 'paragraph',
         'text': '软件采购合同\r\n\r\n\r\n第一条 标的物：甲方向乙方采购软件一套。', 'bbox': None},
        {'seq': 2, 'page_no': 1, 'paragraph_index': 2, 'kind': 'paragraph',
         'text': '第 1 页  共 3 页', 'bbox': None},
        {'seq': 3, 'page_no': 2, 'paragraph_index': 3, 'kind': 'paragraph',
         'text': '某某科技有限公司 合同专用', 'bbox': None},
        {'seq': 4, 'page_no': 2, 'paragraph_index': 4, 'kind': 'paragraph',
         'text': '第二条 付款方式：到货即付全款。   ', 'bbox': None},
        {'seq': 5, 'page_no': 2, 'paragraph_index': 5, 'kind': 'paragraph',
         'text': '第 2 页  共 3 页', 'bbox': None},
        {'seq': 6, 'page_no': 3, 'paragraph_index': 6, 'kind': 'paragraph',
         'text': '某某科技有限公司 合同专用', 'bbox': None},
        {'seq': 7, 'page_no': 3, 'paragraph_index': 7, 'kind': 'paragraph',
         'text': '12', 'bbox': None},
        {'seq': 8, 'page_no': 3, 'paragraph_index': 8, 'kind': 'paragraph',
         'text': '第三条 违约责任：乙方承担全部损失。', 'bbox': None},
    ]

    init_state = get_default_state()
    init_state.update({'task_id': 'task-clean-ok', 'fragments': raw_fragments})
    result = node_clean_text(init_state)
    fragments = result['fragments']
    full_text = result['full_text']

    # 断言 1：跨页重复的页眉被全部删除（3 块）
    assert all('合同专用' not in f['text'] for f in fragments), '跨页重复的页眉应被删除'
    # 断言 2：页码行被删除
    assert all('共 3 页' not in f['text'] for f in fragments), '页码行应被删除'
    assert all(f['text'].strip() != '12' for f in fragments), '纯数字行应被删除'
    # 断言 3：正文完整保留
    joined = ''.join(f['text'] for f in fragments)
    assert '第一条 标的物' in joined and '第二条 付款方式' in joined and '第三条 违约责任' in joined, \
        '正文三个条款都应保留'
    # 断言 4：连续空行被压缩
    assert '\n\n\n' not in joined, '连续空行应被压缩'
    # 断言 5：行尾空白被清掉
    assert not any(line != line.rstrip() for f in fragments for line in f['text'].split('\n')), \
        '不应残留行尾空白'
    # 断言 6：seq 重新连续编号
    assert [f['seq'] for f in fragments] == list(range(len(fragments))), 'seq 应重新连续编号'
    # 断言 7【核心】：全局偏移与 full_text 完全一致
    _assert_offsets_consistent(fragments, full_text)

    passed += 1
    logging.getLogger().info(
        f'  ✅ 页眉页脚/页码被删，正文保留，偏移自洽（{len(fragments)} 块 / {len(full_text)} 字符）')

    # ---------- 二、DOCX 场景：无真实页码时不误删 ----------
    logging.getLogger().info('=' * 22 + ' 二、DOCX（无页码）' + '=' * 22)

    docx_fragments = [
        {'seq': 0, 'page_no': 0, 'paragraph_index': 0, 'kind': 'paragraph',
         'text': '合同专用', 'bbox': None},
        {'seq': 1, 'page_no': 0, 'paragraph_index': 1, 'kind': 'paragraph',
         'text': '合同专用', 'bbox': None},
        {'seq': 2, 'page_no': 0, 'paragraph_index': 2, 'kind': 'paragraph',
         'text': '第一条 标的物。', 'bbox': None},
    ]
    init_state = get_default_state()
    init_state.update({'task_id': 'task-clean-docx', 'fragments': docx_fragments})
    result = node_clean_text(init_state)

    kept_texts = [f['text'] for f in result['fragments']]
    assert kept_texts.count('合同专用') == 2, \
        'DOCX 无真实页码，不应按「跨页重复」误删（可能只是正文里重复出现的短语）'
    _assert_offsets_consistent(result['fragments'], result['full_text'])
    passed += 1
    logging.getLogger().info('  ✅ DOCX 无页码 → 不触发跨页页眉页脚删除')

    # ---------- 三、保护：全块都被判为页眉页脚时必须放弃删除 ----------
    logging.getLogger().info('=' * 22 + ' 三、全块重复保护 ' + '=' * 22)

    # 每一页的正文只有一条"只差页码"的短文本 → 判定规则会把它们全部当成页眉页脚。
    # 此时若照删，整篇就空了；正确行为是放弃删除、保留原块。
    all_duplicated = [
        {'seq': 0, 'page_no': 1, 'paragraph_index': 0, 'kind': 'paragraph', 'bbox': None,
         'text': '[OCR-MOCK] 第 1 页占位文本（未安装 PaddleOCR，未执行真实识别）'},
        {'seq': 1, 'page_no': 2, 'paragraph_index': 1, 'kind': 'paragraph', 'bbox': None,
         'text': '[OCR-MOCK] 第 2 页占位文本（未安装 PaddleOCR，未执行真实识别）'},
    ]
    init_state = get_default_state()
    init_state.update({'task_id': 'task-clean-alldup', 'fragments': all_duplicated})
    result = node_clean_text(init_state)

    assert len(result['fragments']) == 2, \
        f'全块被判为页眉页脚时应放弃删除，实际剩 {len(result["fragments"])} 块'
    assert result['full_text'].strip(), '全文不应被清空'
    _assert_offsets_consistent(result['fragments'], result['full_text'])
    passed += 1
    logging.getLogger().info('  ✅ 全块重复 → 放弃删除，正文未被清空')

    # ---------- 四、配置开关：保留纯数字行 ----------
    logging.getLogger().info('=' * 22 + ' 四、配置开关 ' + '=' * 22)

    loose_node = NodeCleanText(ParseConfig(clean_drop_pure_number_lines=False))
    init_state = get_default_state()
    init_state.update({
        'task_id': 'task-clean-loose',
        'fragments': [
            {'seq': 0, 'page_no': 1, 'paragraph_index': 0, 'kind': 'paragraph',
             'text': '2026', 'bbox': None},
            {'seq': 1, 'page_no': 1, 'paragraph_index': 1, 'kind': 'paragraph',
             'text': '第一条 标的物。', 'bbox': None},
        ],
    })
    result = loose_node(init_state)
    assert any(f['text'] == '2026' for f in result['fragments']), \
        '关闭开关后纯数字行应被保留'
    passed += 1
    logging.getLogger().info('  ✅ clean_drop_pure_number_lines=False → 纯数字行保留')

    # ---------- 五、异常场景 ----------
    logging.getLogger().info('=' * 22 + ' 五、异常场景 ' + '=' * 22)

    err_cases = [
        ('输入块为空', {}, StateFieldError),
        ('清洗后全空',
         {'fragments': [
             {'seq': 0, 'page_no': 1, 'paragraph_index': 0, 'kind': 'paragraph',
              'text': '第 1 页', 'bbox': None},
             {'seq': 1, 'page_no': 1, 'paragraph_index': 1, 'kind': 'paragraph',
              'text': '   ', 'bbox': None},
         ]},
         ValidationError),
    ]

    for case_name, overrides, expected_exc in err_cases:
        init_state = get_default_state()
        init_state['task_id'] = 'task-clean-error'
        init_state.update(overrides)
        try:
            node_clean_text(init_state)
        except Exception as e:
            assert isinstance(e, ParseProcessError), f'{case_name} 应被基类包装成 ParseProcessError'
            assert isinstance(e.cause, expected_exc), \
                f'{case_name} 应抛出 {expected_exc.__name__}，实际为 {type(e.cause).__name__}'
            passed += 1
            logging.getLogger().info(f'  ✅ {case_name:<10} → 正确抛出 {expected_exc.__name__}')
        else:
            raise AssertionError(f'{case_name} 未抛出异常，用例失败')

    # ---------- 六、打印最终状态（便于人工核对）----------
    logging.getLogger().info('=' * 22 + ' 六、最终结果 ' + '=' * 22)
    logging.getLogger().info(json.dumps(
        {'fragments': result['fragments'], 'full_text': result['full_text']},
        ensure_ascii=False, indent=4
    )[:1200])

    logging.getLogger().info(f'node_clean_text 自测全部通过 ✅ 共 {passed} 个用例')
