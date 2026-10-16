'''processor/writeback_processor/comment_template.py'''

'''
回写评论模板（SPEC 10.1）

SPEC 10.1 给的模板原文:

    ## 合同审查意见（系统生成）

    ### 综合结论
    - 风险等级：**高风险**
    - 审查结论：建议整改后通过 / 重大整改后再审

    ### 风险明细
    | 等级 | 风险点 | 涉及条款 | 页码 | 建议 |
    | --- | --- | --- | --- | --- |
    | 高 | 知识产权归属不利 | 第8条 知识产权 | P5 | 见下方修改建议 |

    ### 法务批注
    > {法务批注内容}

    ### 重点关注事项
    1. {…}
    2. {…}

一处对 SPEC 的补充说明（建议列）:
    SPEC 示例里「建议」列写的是「见下方修改建议」，但模板里并没有"下方修改建议"这一节 ——
    推荐引擎产出的建议条款存在 `risk_item.suggestion_text`，没有别的地方会展示它。
    所以这里直接把建议摘要（截断）放进该列；为空时填「—」。
    这样评论本身就是完整的，法务不必再回到系统里找建议。

Markdown 表格的三个坑（都处理了）:
    1. 单元格里的 `|` 会被当成列分隔符 → 替换成全角 `｜`
    2. 单元格里的换行会破表 → 折叠成空格
    3. 单元格过长会把表格撑得没法看 → 按列各设上限并加省略号
'''

import re
from typing import Any, Dict, List, Optional

from db.constants import RiskLevel

# 风险等级 → 中文标签（评论是给人看的，用中文）
_LEVEL_LABELS: Dict[str, str] = {
    RiskLevel.HIGH: '高',
    RiskLevel.MID: '中',
    RiskLevel.LOW: '低',
    RiskLevel.NONE: '—',
}

# 各列的截断上限（字符数）
_CAUSE_LIMIT = 80
_CLAUSE_LIMIT = 40
_SUGGESTION_LIMIT = 60

# 无风险时的提示语 —— 与 node_risk_aggregate 的口径保持一致
NO_RISK_NOTICE = '未检出风险不等于无风险，请人工复核关键条款（金额、付款、违约、知识产权、管辖）'


def level_label(risk_level: Optional[str]) -> str:
    """
    风险等级 → 中文标签
    :param risk_level: high / mid / low / none
    :return: 高 / 中 / 低 / —
    """
    return _LEVEL_LABELS.get((risk_level or '').strip().lower(), '—')


def sanitize_cell(text: Optional[str], limit: int) -> str:
    """
    单元格安全化：去换行、转义竖线、去多余空白、按上限截断
    :param text: 原始文本
    :param limit: 截断上限
    :return: 可安全放进 Markdown 表格单元格的文本
    """
    if not text:
        return '—'
    cleaned = re.sub(r'[\r\n]+', ' ', str(text))
    # 竖线会破坏表格结构，换成全角（视觉上几乎一致）
    cleaned = cleaned.replace('|', '｜')
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
    if not cleaned:
        return '—'
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + '…'
    return cleaned


def clause_label(risk_item: Dict[str, Any]) -> str:
    """
    取「涉及条款」列的展示文本
    :param risk_item: 风险项（含 clause_title / clause_id）
    :return: 条款标题；无标题时退回展示编号；文档级风险返回「整份合同」
    """
    title = (risk_item.get('clause_title') or '').strip()
    if title:
        return sanitize_cell(title, _CLAUSE_LIMIT)
    if risk_item.get('db_clause_id') is None:
        # R-01 这类文档级风险挂不到具体条款上
        return '整份合同'
    return sanitize_cell(risk_item.get('clause_id') or '', _CLAUSE_LIMIT)


def page_label(risk_item: Dict[str, Any]) -> str:
    """
    取「页码」列的展示文本
    :param risk_item: 风险项（含 page_no）
    :return: `P5` 形式；页码为 0（如 DOCX 拿不到真实页码）时返回 «-»
    """
    page_no = risk_item.get('page_no') or 0
    try:
        page_no = int(page_no)
    except (TypeError, ValueError):
        return '-'
    return f'P{page_no}' if page_no > 0 else '-'


def sort_key(risk_item: Dict[str, Any]) -> tuple:
    """
    风险项展示排序：严重度倒序 → 页码升序 → 条款序号升序
    与 node_risk_aggregate 的口径一致，法务从上往下处理即可。
    """
    severity = RiskLevel.SEVERITY.get((risk_item.get('risk_level') or '').lower(), 0)
    return (
        -severity,
        risk_item.get('page_no') or 0,
        risk_item.get('db_clause_id') or 0,
    )


def build_highlights(risk_items: List[Dict[str, Any]], top_n: int,
                     review_note: str = '', writeback_is_mock: bool = False) -> List[str]:
    """
    构建「重点关注事项」

    规则:
        1. 高风险项各占一条，按严重度取前 top_n 条
        2. 审查阶段的降级提示（review_note）一并带出 ——
           覆盖不完整这件事必须让法务看到，否则容易把"没检出"当成"没问题"
        3. **一条风险都没有**时，给出「未检出风险不等于无风险」的提醒
    :param risk_items: 风险项
    :param top_n: 最多列几条高风险事项
    :param review_note: 审查阶段的附加说明（LLM 失败 / mock 等）
    :param writeback_is_mock: 本次回写是否走了 mock Adapter
    :return: 重点关注事项列表（可能为空）
    """
    highlights: List[str] = []

    high_risks = [
        item for item in sorted(risk_items, key=sort_key)
        if (item.get('risk_level') or '').lower() == RiskLevel.HIGH
    ]
    for item in high_risks[:max(0, top_n)]:
        cause = sanitize_cell(item.get('cause'), _CAUSE_LIMIT)
        highlights.append(f'【高风险】{cause}（{clause_label(item)}，{page_label(item)}）')

    if review_note:
        highlights.append(f'【覆盖提示】{review_note}')

    if not risk_items:
        highlights.append(f'【人工复核】{NO_RISK_NOTICE}')

    if writeback_is_mock:
        highlights.append('【链路提示】本次回写走了本地 mock 通道，未真正送达审批系统')

    return highlights


def build_risk_table(risk_items: List[Dict[str, Any]], max_risks: int,
                     include_low: bool = True) -> str:
    """
    渲染 SPEC 10.1 的「风险明细」表格
    :param risk_items: 风险项
    :param max_risks: 最多列多少行
    :param include_low: 是否包含低风险
    :return: Markdown 表格文本
    """
    rows: List[str] = [
        '| 等级 | 风险点 | 涉及条款 | 页码 | 建议 |',
        '| --- | --- | --- | --- | --- |',
    ]

    visible = [
        item for item in sorted(risk_items, key=sort_key)
        if include_low or (item.get('risk_level') or '').lower() != RiskLevel.LOW
    ]
    shown = visible[:max(0, max_risks)]

    if not shown:
        rows.append('| — | 未检出风险 | — | — | — |')
    else:
        for item in shown:
            rows.append(
                f"| {level_label(item.get('risk_level'))} "
                f"| {sanitize_cell(item.get('cause'), _CAUSE_LIMIT)} "
                f"| {clause_label(item)} "
                f"| {page_label(item)} "
                f"| {sanitize_cell(item.get('suggestion_text'), _SUGGESTION_LIMIT)} |"
            )

    # 超出上限时明确告知被截断，而不是悄悄少列（法务需要知道明细不完整）
    truncated = len(visible) - len(shown)
    if truncated > 0:
        rows.append(f'| … | 另有 {truncated} 条风险未在本表列出，请查看系统内完整清单 | — | — | — |')

    return '\n'.join(rows)


def build_manual_comments(risk_items: List[Dict[str, Any]]) -> str:
    """
    渲染「法务批注」段落：把各风险项上的 manual_comment 汇总成引用块
    :param risk_items: 风险项
    :return: Markdown 引用块文本（无批注时给出占位）
    """
    lines: List[str] = []
    for item in sorted(risk_items, key=sort_key):
        comment = (item.get('manual_comment') or '').strip()
        if not comment:
            continue
        lines.append(f'> - **{sanitize_cell(item.get("cause"), _CAUSE_LIMIT)}**：'
                     f'{sanitize_cell(comment, 200)}')

    if not lines:
        return '> （暂无人工批注）'
    return '\n'.join(lines)


def build_comment_markdown(risk_items: List[Dict[str, Any]], overall_risk_level: str,
                           review_conclusion: str, max_risks: int = 50,
                           include_low: bool = True, top_n: int = 3,
                           review_note: str = '', writeback_is_mock: bool = False) -> Dict[str, Any]:
    """
    渲染完整的回写评论（SPEC 10.1 模板）
    :param risk_items: 风险项（含 clause_title / page_no / suggestion_text / manual_comment）
    :param overall_risk_level: 综合风险等级
    :param review_conclusion: 审查结论文案
    :param max_risks: 风险明细表最多行数
    :param include_low: 明细表是否含低风险
    :param top_n: 重点关注事项条数
    :param review_note: 审查阶段的附加说明
    :param writeback_is_mock: 本次回写是否走了 mock Adapter
    :return: {'markdown': str, 'highlights': List[str]}
    """
    highlights = build_highlights(
        risk_items, top_n, review_note=review_note, writeback_is_mock=writeback_is_mock
    )

    highlight_block = '\n'.join(
        f'{index + 1}. {text}' for index, text in enumerate(highlights)
    ) if highlights else '1. （暂无）'

    markdown = f'''## 合同审查意见（系统生成）

### 综合结论
- 风险等级：**{_level_full_label(overall_risk_level)}**
- 审查结论：{review_conclusion or '—'}

### 风险明细
{build_risk_table(risk_items, max_risks, include_low)}

### 法务批注
{build_manual_comments(risk_items)}

### 重点关注事项
{highlight_block}
'''

    return {'markdown': markdown, 'highlights': highlights}


def _level_full_label(risk_level: Optional[str]) -> str:
    """
    综合风险的完整中文标签（用在结论行，比表格里的单字更清楚）
    :param risk_level: high / mid / low / none
    :return: 高风险 / 中风险 / 低风险 / 未评估
    """
    mapping = {
        RiskLevel.HIGH: '高风险',
        RiskLevel.MID: '中风险',
        RiskLevel.LOW: '低风险',
        RiskLevel.NONE: '未评估',
    }
    return mapping.get((risk_level or '').strip().lower(), '未评估')


if __name__ == '__main__':
    # 轻量自测
    passed = 0

    def _risk(risk_id: int, level: str, cause: str, **overrides) -> Dict[str, Any]:
        """构造一条风险项"""
        item = {
            'db_risk_id': risk_id,
            'db_clause_id': risk_id,
            'clause_id': f'cl_{risk_id:03d}',
            'clause_title': f'第{risk_id}条 条款标题',
            'page_no': risk_id,
            'dimension': 'payment',
            'risk_type': 'missing',
            'risk_level': level,
            'cause': cause,
            'suggestion_text': f'建议条款 {risk_id}',
            'manual_comment': '',
        }
        item.update(overrides)
        return item

    # ---------- 一、等级标签与排序 ----------
    assert level_label('high') == '高' and level_label('mid') == '中' and level_label('low') == '低'
    assert level_label('') == '—' and level_label('乱填') == '—'
    assert level_label('HIGH') == '高', '大小写应不敏感'
    passed += 1
    print('  ✅ 风险等级中文标签（含大小写与非法值）')

    items = [
        _risk(3, 'low', '低风险项'),
        _risk(1, 'high', '高风险项甲'),
        _risk(2, 'high', '高风险项乙'),
    ]
    ordered = sorted(items, key=sort_key)
    assert [i['db_risk_id'] for i in ordered] == [1, 2, 3], '应按严重度倒序、同级按页码升序'
    passed += 1
    print('  ✅ 展示排序：严重度倒序 → 页码升序')

    # ---------- 二、单元格安全化（Markdown 表格的坑）----------
    assert sanitize_cell('a|b', 80) == 'a｜b', '竖线必须换成全角，否则破坏表格'
    assert sanitize_cell('第一行\n第二行', 80) == '第一行 第二行', '换行应折叠成空格'
    assert sanitize_cell('  多余   空格  ', 80) == '多余 空格'
    assert sanitize_cell('', 80) == '—'
    assert sanitize_cell(None, 80) == '—'
    long_text = '很长的内容' * 40
    truncated = sanitize_cell(long_text, 20)
    assert len(truncated) == 21 and truncated.endswith('…'), f'应截断并加省略号：{truncated!r}'
    passed += 1
    print('  ✅ 单元格安全化：竖线/换行/空白/超长均已处理')

    # ---------- 三、条款与页码列 ----------
    assert clause_label(_risk(1, 'high', 'x', clause_title='第八条 知识产权')) == '第八条 知识产权'
    assert clause_label({'db_clause_id': None}) == '整份合同', '文档级风险应显示"整份合同"'
    assert clause_label({'db_clause_id': 5, 'clause_id': 'cl_005'}) == 'cl_005', '无标题时退回编号'
    assert page_label({'page_no': 5}) == 'P5'
    assert page_label({'page_no': 0}) == '-', 'DOCX 页码为 0 时应显示 -'
    assert page_label({}) == '-'
    passed += 1
    print('  ✅ 涉及条款与页码列（含文档级风险、页码为 0）')

    # ---------- 四、完整评论渲染 ----------
    result = build_comment_markdown(
        risk_items=[
            _risk(1, 'high', '付款未设验收前置条件', clause_title='第2条 付款方式',
                  manual_comment='必须改为验收后付款'),
            _risk(2, 'mid', '保密期限缺失', clause_title='第5条 保密'),
        ],
        overall_risk_level='high',
        review_conclusion='建议拒绝 / 重大整改后再审',
    )
    markdown = result['markdown']

    for section in ('## 合同审查意见（系统生成）', '### 综合结论', '### 风险明细',
                    '### 法务批注', '### 重点关注事项'):
        assert section in markdown, f'缺少 SPEC 10.1 的章节：{section}'
    assert '风险等级：**高风险**' in markdown
    assert '建议拒绝 / 重大整改后再审' in markdown
    assert '| 等级 | 风险点 | 涉及条款 | 页码 | 建议 |' in markdown, '缺少表头'
    assert '| 高 | 付款未设验收前置条件 | 第2条 付款方式 | P1 | 建议条款 1 |' in markdown, \
        '风险行渲染不正确'
    assert '必须改为验收后付款' in markdown, '法务批注应汇总进评论'
    assert '【高风险】付款未设验收前置条件' in markdown, '高风险应进重点关注事项'
    assert result['highlights'], '应产出重点关注事项'
    passed += 1
    print('  ✅ 完整评论含全部 5 个章节、风险表、批注与重点关注事项')

    # ---------- 五、无风险时的提醒（关键）----------
    empty_result = build_comment_markdown([], overall_risk_level='low', review_conclusion='可通过')
    assert '未检出风险' in empty_result['markdown'], '无风险时应明确写出'
    assert any('未检出风险不等于无风险' in h for h in empty_result['highlights']), \
        '无风险时必须提醒人工复核 —— 否则法务容易把"没检出"当成"没问题"'
    passed += 1
    print('  ✅ 无风险 → 表格与重点关注事项都给出"不等于无风险"的提醒')

    # ---------- 六、降级提示与 mock 提示 ----------
    note_result = build_comment_markdown(
        [_risk(1, 'high', 'x')], overall_risk_level='high',
        review_conclusion='整改后通过', review_note='深度语义审查未完成',
    )
    assert any('覆盖提示' in h and '深度语义审查未完成' in h for h in note_result['highlights']), \
        '审查阶段的降级说明必须带进评论'

    mock_result = build_comment_markdown(
        [_risk(1, 'high', 'y')], overall_risk_level='high',
        review_conclusion='整改后通过',
    )
    assert not any('mock' in h for h in mock_result['highlights']), \
        'writeback_is_mock=False 时不应出现 mock 提示'

    mock_result = build_comment_markdown(
        [_risk(1, 'high', 'y')], overall_risk_level='high',
        review_conclusion='整改后通过', writeback_is_mock=True,
    )
    assert any('mock' in h and '未真正送达' in h for h in mock_result['highlights']), \
        '走 mock 通道时必须明确告知审批系统那边并没有真正收到'
    passed += 1
    print('  ✅ 降级说明带进评论；mock 通道会明确提示"未真正送达"')

    # ---------- 七、超量截断 ----------
    many = [_risk(i, 'mid', f'风险{i}') for i in range(1, 11)]
    limited = build_comment_markdown(many, overall_risk_level='mid',
                                    review_conclusion='整改后通过', max_risks=3)
    assert '另有 7 条风险未在本表列出' in limited['markdown'], \
        '超出上限必须显式告知，不能悄悄少列'
    passed += 1
    print('  ✅ 风险超过上限时显式标注被截断的条数')

    # ---------- 八、低风险过滤开关 ----------
    mixed = [_risk(1, 'high', '高风险'), _risk(2, 'low', '低风险')]
    without_low = build_comment_markdown(mixed, overall_risk_level='high',
                                        review_conclusion='整改后通过', include_low=False)
    assert '低风险' not in without_low['markdown'], 'include_low=False 时不应出现低风险行'
    assert '高风险' in without_low['markdown']
    with_low = build_comment_markdown(mixed, overall_risk_level='high',
                                     review_conclusion='整改后通过', include_low=True)
    assert '低风险' in with_low['markdown'], 'include_low=True 时应出现低风险行'
    passed += 1
    print('  ✅ include_low 开关生效')

    print(f'comment_template 自测全部通过 ✅ 共 {passed} 个用例')
