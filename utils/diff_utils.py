'''utils/diff_utils.py'''

"""
文本差异对比工具（SPEC 4.4：原文 vs 建议「行级 diff」）

为什么不是字面意义的"行级":
    SPEC 写的是「行级 diff」，但中文合同正文通常是一整段一坨文本，
    按 \\n 切分往往只有 1 行，diff 出来就是"整行删除 + 整行新增"，看不出改了哪里。
    所以这里按**句子**切分（句末标点 。；！？… 以及换行）再比对 ——
    阅读体验等同于行级，对中文合同才真正有用。

算法:
    LCS（最长公共子序列）动态规划，O(n*m)。句子数通常几十条，完全够用。
    为防止极端输入（上千句）把 O(n*m) 撑爆，超过阈值时退化为「整体替换」
    （一段 delete + 一段 insert），保证不卡死也不吃光内存。

输出结构:
    {
      'segments': [{'op': 'equal'|'delete'|'insert', 'text': '...'}, ...],
      'stats': {
          'original_chars': int,    # 原文长度
          'suggested_chars': int,   # 建议长度
          'added_chars': int,       # 新增字符数
          'removed_chars': int,     # 删除字符数
          'changed': bool,          # 是否有实质改动
          'changed_ratio': float,   # (新增+删除) / (原文+建议)，0~1，越大改动越多
      },
    }
    segments 的顺序即阅读顺序，前端按 op 上色
    （delete 红色删除线 / insert 绿色底 / equal 常规）。

使用限制:
    本模块是**纯函数**工具，不依赖数据库与 LLM，可被任意 processor 复用。
"""

from typing import Any, Dict, List, Tuple

# 句末标点：遇到这些字符即认为一句话结束
_SENTENCE_END = '。；！？；;!?…'
# 右闭合符号：紧跟句末标点的引号/括号应归入上一句，否则会出现孤立的 ” 」 碎片
_CLOSERS = '”』」"\')）】》'

# LCS 单元格数上限，超过则退化为整体替换（40000 ≈ 200 句 × 200 句）
DEFAULT_MAX_CELLS = 40000


def split_sentences(text: str) -> List[str]:
    """
    把文本切成句子序列
    功能：先按换行分段，再在每段内按句末标点切句；紧跟的右引号/右括号归入上一句
    :param text: 原始文本
    :return: 句子列表（已去空白，不含空串）
    """
    if not text:
        return []

    sentences: List[str] = []
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue

        buffer = ''
        for char in line:
            buffer += char
            if char in _SENTENCE_END:
                piece = buffer.strip()
                if piece:
                    sentences.append(piece)
                buffer = ''

        piece = buffer.strip()
        if piece:
            sentences.append(piece)

    # 后处理：把以右闭合符号开头的碎片合并回上一句
    merged: List[str] = []
    for piece in sentences:
        if merged and piece[0] in _CLOSERS:
            merged[-1] += piece
        else:
            merged.append(piece)
    return merged


def _build_lcs_table(left: List[str], right: List[str]) -> List[List[int]]:
    """
    【辅助函数】构建 LCS 动态规划表
    :param left: 序列 A（原文句子）
    :param right: 序列 B（建议句子）
    :return: (len(left)+1) x (len(right)+1) 的表格
    """
    rows, cols = len(left), len(right)
    # 从后往前填，dp[i][j] 表示 left[i:] 与 right[j:] 的 LCS 长度
    table = [[0] * (cols + 1) for _ in range(rows + 1)]

    for i in range(rows - 1, -1, -1):
        current, below = table[i], table[i + 1]
        for j in range(cols - 1, -1, -1):
            if left[i] == right[j]:
                current[j] = below[j + 1] + 1
            else:
                current[j] = below[j] if below[j] >= current[j + 1] else current[j + 1]
    return table


def _lcs_ops(left: List[str], right: List[str]) -> List[Tuple[str, str]]:
    """
    【辅助函数】回溯 LCS 表，得到 (操作, 句子) 序列
    :param left: 原文句子序列
    :param right: 建议句子序列
    :return: [('equal'|'delete'|'insert', 句子), ...]
    """
    table = _build_lcs_table(left, right)
    ops: List[Tuple[str, str]] = []
    i = j = 0

    while i < len(left) and j < len(right):
        if left[i] == right[j]:
            ops.append(('equal', left[i]))
            i += 1
            j += 1
        elif table[i + 1][j] >= table[i][j + 1]:
            ops.append(('delete', left[i]))
            i += 1
        else:
            ops.append(('insert', right[j]))
            j += 1

    while i < len(left):
        ops.append(('delete', left[i]))
        i += 1
    while j < len(right):
        ops.append(('insert', right[j]))
        j += 1

    return ops


def _merge_ops(ops: List[Tuple[str, str]]) -> List[Dict[str, str]]:
    """
    【辅助函数】把相邻同类操作合并成段落
    功能：['delete','delete','insert'] → 两个 segment，前端渲染更省 DOM
    :param ops: 操作序列
    :return: segments 列表
    """
    segments: List[Dict[str, str]] = []
    for op, text in ops:
        if segments and segments[-1]['op'] == op:
            segments[-1]['text'] += text
        else:
            segments.append({'op': op, 'text': text})
    return segments


def diff_text(original: str, suggested: str,
              max_cells: int = DEFAULT_MAX_CELLS) -> Dict[str, Any]:
    """
    对比两段文本，产出结构化差异
    :param original: 原文（合同条款正文）
    :param suggested: 建议（推荐改写后的条款）
    :param max_cells: LCS 单元格上限，超过则退化为整体替换
    :return: {'segments': [...], 'stats': {...}}
    """
    original = original or ''
    suggested = suggested or ''

    original_sentences = split_sentences(original)
    suggested_sentences = split_sentences(suggested)

    if not original_sentences and not suggested_sentences:
        ops: List[Tuple[str, str]] = []
    elif len(original_sentences) * len(suggested_sentences) > max_cells:
        # 退化为整体替换：宁可 diff 粒度粗，也不能让页面卡死
        ops = [('delete', s) for s in original_sentences]
        ops += [('insert', s) for s in suggested_sentences]
    else:
        ops = _lcs_ops(original_sentences, suggested_sentences)

    segments = _merge_ops(ops)

    added_chars = sum(len(seg['text']) for seg in segments if seg['op'] == 'insert')
    removed_chars = sum(len(seg['text']) for seg in segments if seg['op'] == 'delete')
    denominator = max(1, len(original) + len(suggested))

    return {
        'segments': segments,
        'stats': {
            'original_chars': len(original),
            'suggested_chars': len(suggested),
            'added_chars': added_chars,
            'removed_chars': removed_chars,
            'changed': bool(added_chars or removed_chars),
            'changed_ratio': round((added_chars + removed_chars) / denominator, 4),
        },
    }


if __name__ == '__main__':
    # 轻量自测
    passed = 0

    # ---------- 一、分句 ----------
    # 说明：`；` 也会切开 —— 合同里 `；` 分隔的通常是并行的独立义务，
    #      细粒度 diff 才能定位到"改的是哪一条义务"。
    sentences = split_sentences('甲方应在验收合格后付款。乙方应提供发票；否则甲方有权拒付。')
    assert len(sentences) == 3, f'应切成 3 句，实际 {len(sentences)} 句：{sentences}'
    assert sentences[0] == '甲方应在验收合格后付款。'
    assert sentences[1] == '乙方应提供发票；'
    assert sentences[2] == '否则甲方有权拒付。'
    passed += 1

    # 右引号/右括号应归入上一句，不产生孤立碎片
    quoted = split_sentences('双方确认“验收合格”。（详见附件一）')
    assert len(quoted) == 2, f'右闭合符号应跟随上一句，实际 {quoted}'
    assert quoted[0] == '双方确认“验收合格”。'
    assert quoted[1] == '（详见附件一）'

    # 换行也分段；空行与空白应被丢弃
    assert split_sentences('第一行\n\n  \n第二行') == ['第一行', '第二行']
    assert split_sentences('') == []
    assert split_sentences(None) == []
    passed += 1

    # ---------- 二、完全相同 ----------
    same = diff_text('甲方应付款。', '甲方应付款。')
    assert same['stats']['changed'] is False, '相同文本不应有改动'
    assert same['stats']['added_chars'] == 0 and same['stats']['removed_chars'] == 0
    assert len(same['segments']) == 1 and same['segments'][0]['op'] == 'equal'
    passed += 1

    # ---------- 三、局部改写（核心场景）----------
    original = '甲方应在收到货物后 3 日内支付全款。乙方不承担任何违约责任。'
    suggested = '甲方应在验收合格后 15 个工作日内支付合同款项。乙方应按合同总额的 10% 承担违约责任。'
    result = diff_text(original, suggested)

    ops_used = {seg['op'] for seg in result['segments']}
    assert 'delete' in ops_used, '改写应产生删除段'
    assert 'insert' in ops_used, '改写应产生新增段'
    assert result['stats']['changed'] is True
    assert 0 < result['stats']['changed_ratio'] <= 1, \
        f'改动比例应在 0~1，实际 {result["stats"]["changed_ratio"]}'
    passed += 1

    # ---------- 四、部分保留 ----------
    half = diff_text(
        '甲方应在验收合格后 15 个工作日内支付。乙方应按合同总额的 10% 承担违约责任。',
        '甲方应在验收合格后 15 个工作日内支付。乙方不承担任何责任。',
    )
    equal_segments = [seg for seg in half['segments'] if seg['op'] == 'equal']
    assert equal_segments, '未改动的那句应被识别为 equal 并保留'
    assert any('15 个工作日内支付' in seg['text'] for seg in equal_segments)
    passed += 1

    # ---------- 五、空文本边界 ----------
    from_empty = diff_text('', '全新的建议条款。')
    assert from_empty['stats']['removed_chars'] == 0
    assert from_empty['stats']['added_chars'] > 0
    assert [seg['op'] for seg in from_empty['segments']] == ['insert']

    to_empty = diff_text('原有的条款。', '')
    assert [seg['op'] for seg in to_empty['segments']] == ['delete']

    both_empty = diff_text('', '')
    assert both_empty['segments'] == [] and both_empty['stats']['changed'] is False
    passed += 1

    # ---------- 六、超阈值退化不卡死 ----------
    huge_original = '\n'.join(f'第{i}句话内容。' for i in range(300))
    huge_suggested = '\n'.join(f'第{i}句新内容。' for i in range(300))
    degraded = diff_text(huge_original, huge_suggested, max_cells=100)
    # 退化后应是两段：整块删除 + 整块新增
    assert [seg['op'] for seg in degraded['segments']] == ['delete', 'insert'], \
        f'超阈值应退化为整体替换，实际 {[seg["op"] for seg in degraded["segments"]]}'
    passed += 1

    # ---------- 七、max_cells 内正常走 LCS（不退化）----------
    normal = diff_text('第一句。第二句。', '第一句。第三句。', max_cells=100)
    assert [seg['op'] for seg in normal['segments']] == ['equal', 'delete', 'insert'], \
        f'应识别公共前缀，实际 {[seg["op"] for seg in normal["segments"]]}'
    passed += 1

    print(f'diff_utils 自测全部通过 ✅ 共 {passed} 个用例')
