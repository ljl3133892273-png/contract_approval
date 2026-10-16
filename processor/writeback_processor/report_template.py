'''processor/writeback_processor/report_template.py'''

'''
审查报告模板（SPEC 10.2）

SPEC 10.2 规定的报告区块与数据源:

    | 区块            | 数据源 |
    | 合同基本信息     | contract_meta + task |
    | 综合审查结论     | task.risk_level + 映射结论 |
    | 风险清单明细     | risk_item × clause（含 Locator 页码）|
    | 审批回写评论文本 | review_comment.content_md |
    | 法务批注         | risk_item.manual_comment |

对 SPEC 的一处补充（「修改建议」区块）:
    `risk_item.suggestion_text` 是推荐引擎的核心产物，也是法务真正会拿去改合同的东西，
    但 SPEC 10.2 的区块表里没有它的位置（表格里塞不下长文本）。
    所以这里额外加了一节「修改建议」，逐条列出 原文 → 建议 → 差异规模。
    不加这一节的话，导出的报告就看不到建议条款，而它恰恰是最有价值的部分。

报告是给**归档与流转**用的，所以刻意做成自解释的：
即使脱离系统，只看这份文档也能知道"审了什么、结论是什么、要改什么、谁写了批注"。
'''

from datetime import datetime
from typing import Any, Dict, List, Optional

from db.constants import RiskLevel, RiskSource
from processor.writeback_processor.comment_template import (
    clause_label,
    level_label,
    page_label,
    sanitize_cell,
    sort_key,
)
from utils.diff_utils import diff_text

# 各字段在该表里的展示上限
_FIELD_LIMIT = 120
_CAUSE_LIMIT = 120
_SUGGESTION_PREVIEW = 500

_LEVEL_FULL = {
    RiskLevel.HIGH: '高风险',
    RiskLevel.MID: '中风险',
    RiskLevel.LOW: '低风险',
    RiskLevel.NONE: '未评估',
}

_SOURCE_LABEL = {
    RiskSource.RULE: '规则引擎',
    RiskSource.LLM: '语义审查',
    RiskSource.MANUAL: '人工录入',
}

_DIMENSION_LABEL = {
    'subject': '主体资质', 'payment': '金额支付', 'breach': '违约责任', 'ip': '知识产权',
    'data_security': '数据安全', 'confidentiality': '保密', 'jurisdiction': '争议管辖',
    'acceptance': '验收标准',
}

_RISK_TYPE_LABEL = {
    'missing': '必备条款缺失', 'unbalanced': '权责不对等', 'high_risk_exp': '高风险表述',
    'amount': '金额相关', 'timing': '时限相关',
}

_STATUS_LABEL = {
    'pending': '待处理', 'parsing': '解析中', 'reviewing': '审查中',
    'completed': '审查完成', 'blocked': '已阻塞',
}

_WRITEBACK_LABEL = {
    'not_written': '未回写', 'writing': '回写中', 'success': '回写成功', 'failed': '回写失败',
}


def _cell(text: Any, limit: int = _FIELD_LIMIT) -> str:
    """字段安全化（复用评论模板的单元格处理，保持两处口径一致）"""
    return sanitize_cell('' if text is None else str(text), limit)


def _label(mapping: Dict[str, str], key: Optional[str], default: str = '—') -> str:
    """按映射取中文标签"""
    return mapping.get((key or '').strip().lower(), default)


def _build_basic_info_table(task: Dict[str, Any], meta: Dict[str, Any]) -> str:
    """一、合同基本信息"""
    rows = [
        ('合同名称', _cell(task.get('contract_name'))),
        ('合同编号', _cell(meta.get('contract_no'))),
        ('审批单号', _cell(task.get('external_doc_id') or '（非审批来源）')),
        ('我方（甲方）', _cell(meta.get('party_a'))),
        ('相对方（乙方）', _cell(meta.get('party_b'))),
        ('统一社会信用代码', _cell(meta.get('credit_code'))),
        ('合同金额', _cell(_format_amount(meta))),
        ('履行期限', _cell(_format_period(meta))),
        ('生效条件', _cell(meta.get('effective_condition'))),
        ('合同类型', _cell(task.get('biz_type') or '（未识别）')),
        ('接入来源', _cell(_label({
            'upload': '本地上传', 'approve_push': '审批推送', 'approve_pull': '审批拉取',
        }, task.get('source_type'), '—'))),
        ('文件类型', _cell(task.get('file_type'))),
    ]
    body = '\n'.join(f'| {name} | {value} |' for name, value in rows)
    return f'| 项目 | 内容 |\n| --- | --- |\n{body}'


def _format_amount(meta: Dict[str, Any]) -> str:
    """金额 + 币种"""
    amount = meta.get('amount')
    if amount in (None, ''):
        return '—'
    try:
        formatted = f'{float(amount):,.2f}'
    except (TypeError, ValueError):
        formatted = str(amount)
    currency = (meta.get('currency') or '').strip()
    return f'{formatted} {currency}'.strip()


def _format_period(meta: Dict[str, Any]) -> str:
    """履行期限（起止拼接）"""
    start = (meta.get('period_start') or '').strip()
    end = (meta.get('period_end') or '').strip()
    if not start and not end:
        return '—'
    if start and end:
        return f'{start} 至 {end}'
    return start or end


def _build_conclusion_table(task: Dict[str, Any], overall_risk_level: str,
                            review_conclusion: str, review_note: str,
                            writeback_status: str) -> str:
    """二、综合审查结论"""
    rows = [
        ('综合风险等级', _level_full(overall_risk_level)),
        ('审查结论', _cell(review_conclusion)),
        ('审查状态', _label(_STATUS_LABEL, task.get('review_status'))),
        ('回写状态', _label(_WRITEBACK_LABEL, writeback_status)),
    ]
    if task.get('blocked_reason'):
        rows.append(('阻塞原因', _cell(task.get('blocked_reason'))))
    if review_note:
        rows.append(('覆盖说明', _cell(review_note, 300)))
    body = '\n'.join(f'| {name} | {value} |' for name, value in rows)
    return f'| 项目 | 内容 |\n| --- | --- |\n{body}'


def _level_full(risk_level: Optional[str]) -> str:
    """综合风险中文标签"""
    return _LEVEL_FULL.get((risk_level or '').strip().lower(), '未评估')


def _build_risk_table(risk_items: List[Dict[str, Any]]) -> str:
    """三、风险清单明细"""
    header = ('| # | 等级 | 维度 | 风险类型 | 涉及条款 | 页码 | 风险成因 | 合规依据 | 来源 |\n'
              '| --- | --- | --- | --- | --- | --- | --- | --- | --- |')
    if not risk_items:
        return header + '\n| — | — | — | — | — | — | 未检出风险 | — | — |'

    rows = []
    for index, item in enumerate(sorted(risk_items, key=sort_key), start=1):
        rows.append(
            f"| {index} "
            f"| {level_label(item.get('risk_level'))} "
            f"| {_label(_DIMENSION_LABEL, item.get('dimension'))} "
            f"| {_label(_RISK_TYPE_LABEL, item.get('risk_type'))} "
            f"| {clause_label(item)} "
            f"| {page_label(item)} "
            f"| {_cell(item.get('cause'), _CAUSE_LIMIT)} "
            f"| {_cell(item.get('legal_basis'), 80)} "
            f"| {_label(_SOURCE_LABEL, item.get('source'))} |"
        )
    return header + '\n' + '\n'.join(rows)


def _build_suggestion_sections(risk_items: List[Dict[str, Any]]) -> str:
    """四、修改建议（逐条给出 原文 → 建议 + 差异规模）"""
    with_suggestion = [
        item for item in sorted(risk_items, key=sort_key)
        if (item.get('suggestion_text') or '').strip()
    ]
    if not with_suggestion:
        return '本次审查未产出修改建议条款。'

    blocks: List[str] = []
    for index, item in enumerate(with_suggestion, start=1):
        suggestion = (item.get('suggestion_text') or '').strip()
        original = (item.get('clause_content') or '').strip()

        diff = diff_text(original, suggestion)
        stats = diff['stats']
        if not original:
            diff_summary = '原文为空（文档级风险），本条为新增条款'
        elif not stats['changed']:
            diff_summary = '与原文一致，未产生实际修改'
        else:
            diff_summary = f"新增 {stats['added_chars']} 字 / 删除 {stats['removed_chars']} 字"

        blocks.append(f'''### {index}. {_cell(item.get('cause'), _CAUSE_LIMIT)}

- **风险等级**：{_level_full(item.get('risk_level'))}　**涉及条款**：{clause_label(item)}　**页码**：{page_label(item)}
- **差异概览**：{diff_summary}

**原文**

```
{original or '（无对应条款原文）'}
```

**建议条款**

```
{suggestion[:_SUGGESTION_PREVIEW]}{'…（已截断，完整内容见系统）' if len(suggestion) > _SUGGESTION_PREVIEW else ''}
```
''')
    return '\n'.join(blocks)


def _build_manual_comments_section(risk_items: List[Dict[str, Any]]) -> str:
    """五、法务批注"""
    lines = []
    for item in sorted(risk_items, key=sort_key):
        comment = (item.get('manual_comment') or '').strip()
        if not comment:
            continue
        lines.append(f"- **{_cell(item.get('cause'), _CAUSE_LIMIT)}**"
                     f"（{clause_label(item)}）：{_cell(comment, 300)}")
    return '\n'.join(lines) if lines else '本次审查暂无法务人工批注。'


def build_report_markdown(task: Dict[str, Any], meta: Dict[str, Any],
                          risk_items: List[Dict[str, Any]], overall_risk_level: str,
                          review_conclusion: str, review_note: str = '',
                          review_comment_md: str = '', writeback_status: str = '',
                          generated_at: Optional[str] = None,
                          title: str = '合同审查报告') -> str:
    """
    渲染完整的审查报告（SPEC 10.2 的五个区块 + 一个补充区块）
    :param task: contract_task 快照
    :param meta: contract_meta 快照
    :param risk_items: 风险项（建议带上 clause_title / page_no / clause_content /
                       suggestion_text / manual_comment，这样报告才自解释）
    :param overall_risk_level: 综合风险等级
    :param review_conclusion: 审查结论文案
    :param review_note: 覆盖情况说明（LLM 降级等）
    :param review_comment_md: 已回写的评论文本
    :param writeback_status: 回写状态
    :param generated_at: 生成时间（默认当前时间）
    :param title: 报告标题
    :return: Markdown 报告全文
    """
    generated_at = generated_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    contract_name = (task.get('contract_name') or '').strip() or '（未命名合同）'

    # 风险等级统计（给结论区一个速览）
    level_counter: Dict[str, int] = {}
    for item in risk_items:
        key = (item.get('risk_level') or 'unknown').lower()
        level_counter[key] = level_counter.get(key, 0) + 1
    statistics = (
        f'共 {len(risk_items)} 条风险'
        f'（高 {level_counter.get(RiskLevel.HIGH, 0)} / '
        f'中 {level_counter.get(RiskLevel.MID, 0)} / '
        f'低 {level_counter.get(RiskLevel.LOW, 0)}）'
    ) if risk_items else '未检出风险'

    comment_section = (review_comment_md or '').strip() or '（尚未回写审批意见）'

    return f'''# {title}

> 合同：{contract_name}　|　风险总览：{statistics}　|　生成时间：{generated_at}

## 一、合同基本信息

{_build_basic_info_table(task, meta)}

## 二、综合审查结论

{_build_conclusion_table(task, overall_risk_level, review_conclusion,
                         review_note, writeback_status)}

## 三、风险清单明细

{_build_risk_table(risk_items)}

## 四、修改建议

{_build_suggestion_sections(risk_items)}

## 五、法务批注

{_build_manual_comments_section(risk_items)}

## 六、审批回写评论

{comment_section}

---

*本报告由合同审批审查系统自动生成，风险判定结果供法务复核参考，最终意见以法务人工审定为准。*
'''


if __name__ == '__main__':
    # 轻量自测
    passed = 0

    task = {
        'contract_name': '软件采购合同', 'external_doc_id': 'APV-2026-001',
        'biz_type': '采购', 'source_type': 'approve_push', 'file_type': 'docx',
        'review_status': 'completed', 'blocked_reason': None,
    }
    meta = {
        'contract_no': 'CG-2026-0001', 'party_a': '某某科技有限公司',
        'party_b': '某某软件股份有限公司', 'credit_code': '91310000MA1FL5678Y',
        'amount': 1280000, 'currency': 'CNY',
        'period_start': '2026-01-01', 'period_end': '2026-12-31',
        'effective_condition': '双方签字盖章之日起生效',
    }
    risk_items = [
        {
            'db_risk_id': 1, 'db_clause_id': 2, 'clause_id': 'cl_002',
            'clause_title': '第二条 付款方式', 'page_no': 1,
            'clause_content': '货到即付全款，未设验收条件。',
            'dimension': 'payment', 'risk_type': 'timing', 'risk_level': 'high',
            'cause': '付款未设验收前置条件', 'legal_basis': '《民法典》第626条',
            'suggestion_text': '甲方应在验收合格后 15 个工作日内支付合同款项。',
            'source': 'rule', 'manual_comment': '必须改为验收后付款',
        },
        {
            'db_risk_id': 2, 'db_clause_id': None, 'clause_id': '',
            'clause_title': '', 'page_no': 0, 'clause_content': '',
            'dimension': 'subject', 'risk_type': 'missing', 'risk_level': 'high',
            'cause': '相对方统一社会信用代码缺失', 'legal_basis': '《民法典》第502条',
            'suggestion_text': '第一条 主体资质：乙方应提供年检有效的营业执照。',
            'source': 'llm', 'manual_comment': '',
        },
    ]

    report = build_report_markdown(
        task, meta, risk_items, overall_risk_level='high',
        review_conclusion='建议拒绝 / 重大整改后再审',
        review_note='深度语义审查未完成（LLM 降级）',
        review_comment_md='## 合同审查意见（系统生成）\n- 风险等级：**高风险**',
        writeback_status='success',
    )

    # ---------- 一、SPEC 10.2 的区块是否齐全 ----------
    for section in ('# 合同审查报告', '## 一、合同基本信息', '## 二、综合审查结论',
                    '## 三、风险清单明细', '## 四、修改建议', '## 五、法务批注',
                    '## 六、审批回写评论'):
        assert section in report, f'缺少区块：{section}'
    passed += 1
    print('  ✅ SPEC 10.2 五个区块 + 补充的修改建议区块齐全')

    # ---------- 二、关键数据是否落到报告里 ----------
    assert '软件采购合同' in report and 'CG-2026-0001' in report
    assert '1,280,000.00 CNY' in report, '金额应带千分位与币种'
    assert '2026-01-01 至 2026-12-31' in report, '履行期限应拼成区间'
    assert '高风险' in report and '建议拒绝 / 重大整改后再审' in report
    assert '审查完成' in report, '状态应转中文'
    assert '回写成功' in report
    assert '深度语义审查未完成' in report, '覆盖说明应带出'
    assert '必须改为验收后付款' in report, '法务批注应出现在报告里'
    assert '## 合同审查意见（系统生成）' in report, '回写的评论应原样附上'
    passed += 1
    print('  ✅ 基本信息 / 结论 / 状态中文映射 / 批注 / 回写评论均已落入报告')

    # ---------- 三、风险明细与修改建议 ----------
    # 断言不写死行号：排序是按 严重度 → 页码，文档级风险（page_no=0）会排在最前
    assert '| 高 | 金额支付 | 时限相关 | 第二条 付款方式 | P1 |' in report, \
        '风险明细行渲染不正确（维度与类型应转中文）'
    assert '| 高 | 主体资质 | 必备条款缺失 | 整份合同 | - |' in report, \
        '文档级风险应显示"整份合同"、页码显示 - '
    assert '规则引擎' in report and '语义审查' in report, '来源应转中文'
    assert '原文为空（文档级风险），本条为新增条款' in report, \
        '文档级风险的差异概览应说明是新增条款'
    assert '新增' in report and '删除' in report, '有改动的条目应给出差异规模'
    passed += 1
    print('  ✅ 风险明细中文映射正确，修改建议含差异概览')

    # ---------- 四、无风险 / 无建议 / 未回写 ----------
    empty_report = build_report_markdown({}, {}, [], overall_risk_level='low',
                                        review_conclusion='可通过')
    assert '未检出风险' in empty_report
    assert '本次审查未产出修改建议条款' in empty_report
    assert '尚未回写审批意见' in empty_report
    assert '（未命名合同）' in empty_report, '合同名为空时要有占位'
    assert '本次审查暂无法务人工批注' in empty_report
    passed += 1
    print('  ✅ 无风险 / 无建议 / 未回写 / 无批注 都能正常渲染并给出占位')

    # ---------- 五、表格安全化（竖线不能破表）----------
    tricky = dict(risk_items[0])
    tricky['cause'] = '含竖线 | 的成因'
    tricky['clause_title'] = '标题 | 带竖线'
    tricky_report = build_report_markdown(task, meta, [tricky],
                                         overall_risk_level='high', review_conclusion='x')
    assert '含竖线 ｜ 的成因' in tricky_report, '成因里的竖线应转全角，否则破坏表格'
    assert '标题 ｜ 带竖线' in tricky_report
    passed += 1
    print('  ✅ 含竖线的文本被安全转义，不会破坏 Markdown 表格')

    # ---------- 六、长建议被截断 ----------
    long_item = dict(risk_items[0])
    long_item['suggestion_text'] = '条款内容' * 300
    long_report = build_report_markdown(task, meta, [long_item],
                                       overall_risk_level='high', review_conclusion='x')
    assert '已截断，完整内容见系统' in long_report, '超长建议应截断并说明'
    passed += 1
    print('  ✅ 超长建议截断并提示"完整内容见系统"')

    print(f'report_template 自测全部通过 ✅ 共 {passed} 个用例')
