'''processor/review_processor/prompt/review_prompt.py'''

'''
P3 —— 条款审查提示词（SPEC 8.2）

输入: 审查维度系统提示词 + 条款文本 + 规则摘要
输出: RiskItem[]（JSON）

设计要点:
1. **强约束 JSON**：明确给出 schema，并要求"没有问题就返回空数组"，
   避免模型为了凑数编造风险（SPEC 16 风险表里点名了 LLM 幻觉问题）。
2. **把规则层已命中的风险作为参考喂进去**，并要求"不要重复报告"，
   让 LLM 专注于规则覆盖不到的语义风险。
3. **维度与风险类型用固定枚举**，与 db/constants.py 保持一致，
   避免模型自由发挥导致下游落库字段非法。
4. 全部要求中文输出，且限制每条不超过 200 字 —— 因为 cause / legal_basis
   在库里分别是 VARCHAR(512)，suggestion 是 TEXT 但也不宜过长。
'''

from typing import Any, Dict, List

# 审查维度（与 db.constants.RiskDimension 保持一致）
DIMENSIONS = (
    'subject', 'payment', 'breach', 'ip',
    'data_security', 'confidentiality', 'jurisdiction', 'acceptance',
)

# 风险类型（与 db.constants.RiskType 保持一致）
RISK_TYPES = ('missing', 'unbalanced', 'high_risk_exp', 'amount', 'timing')

# 风险等级（与 db.constants.RiskLevel 保持一致）
RISK_LEVELS = ('high', 'mid', 'low')

SYSTEM_PROMPT = f'''你是一名资深企业法务，负责审查企业合同的法律合规风险与商业风险。
请针对给定的【单条合同条款】进行分析，只输出严格的 JSON，不要输出任何解释性文字、不要使用 markdown 代码块。

审查维度（dimension）只能取以下之一：
- subject 主体资质
- payment 金额支付
- breach 违约责任
- ip 知识产权
- data_security 数据安全
- confidentiality 保密义务
- jurisdiction 争议管辖
- acceptance 验收标准

风险类型（risk_type）只能取以下之一：
- missing 必备条款缺失
- unbalanced 权责不对等
- high_risk_exp 高风险表述
- amount 金额相关
- timing 时限相关

风险等级（risk_level）只能取以下之一：high / mid / low

输出格式：
{{
  "items": [
    {{
      "dimension": "payment",
      "risk_type": "timing",
      "risk_level": "high",
      "cause": "风险成因，简明扼要",
      "legal_basis": "法律或标准依据",
      "suggestion": "可落地的修改建议"
    }}
  ]
}}

硬性要求：
1. 若该条款未发现风险，必须返回 {{"items": []}}，**不要为了凑数编造风险**。
2. cause / legal_basis / suggestion 一律使用中文，每项不超过 200 字。
3. 不要重复报告【已由规则引擎命中的风险】里已经列出的问题。
4. risk_level 必须严格取自 high / mid / low，dimension 与 risk_type 同样只能取上面列出的值。
5. 只输出 JSON 本体，不要包裹代码块围栏。'''


def build_user_prompt(clause: Dict[str, Any], meta: Dict[str, Any],
                      contract_type: str,
                      rule_hints: List[str] = None) -> str:
    """
    构建 P3 的用户消息

    :param clause: 待审查条款（含 clause_type / title / content）
    :param meta: 合同元数据（party_a / party_b / amount / currency）
    :param contract_type: 合同类型
    :param rule_hints: 规则层已命中的风险描述（供模型避让，避免重复报告）
    :return: 用户提示词文本
    """
    rule_hints = rule_hints or []
    hint_text = '\n'.join(f'- {hint}' for hint in rule_hints) if rule_hints else '（无）'

    amount = meta.get('amount')
    amount_text = f'{amount} {meta.get("currency") or ""}'.strip() if amount else '未提取到'

    return f'''【合同基本信息】
合同类型：{contract_type}
我方（甲方）：{meta.get('party_a') or '未提取到'}
相对方（乙方）：{meta.get('party_b') or '未提取到'}
合同金额：{amount_text}

【已由规则引擎命中的风险（仅供参考，不要重复报告）】
{hint_text}

【待审查条款】
条款类型：{clause.get('clause_type') or 'other'}
条款标题：{clause.get('title') or '(无标题)'}
条款正文：
{clause.get('content') or ''}

请输出 JSON。'''


def build_review_messages(clause: Dict[str, Any], meta: Dict[str, Any],
                          contract_type: str,
                          rule_hints: List[str] = None) -> List[Dict[str, str]]:
    """
    构建完整的 P3 对话消息（system + user）
    :return: [{'role': 'system', 'content': ...}, {'role': 'user', 'content': ...}]
    """
    return [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': build_user_prompt(clause, meta, contract_type, rule_hints)},
    ]


if __name__ == '__main__':
    # 轻量自测：提示词必须包含枚举与硬性约束
    for token in DIMENSIONS + RISK_TYPES + RISK_LEVELS:
        assert token in SYSTEM_PROMPT, f'系统提示词缺少枚举值 {token}'
    assert 'items' in SYSTEM_PROMPT, '系统提示词必须声明输出 schema'
    assert '不要为了凑数编造风险' in SYSTEM_PROMPT, '必须有反幻觉约束'

    messages = build_review_messages(
        clause={'clause_type': 'payment', 'title': '第二条 付款方式', 'content': '货到即付全款。'},
        meta={'party_a': '甲公司', 'party_b': '乙公司', 'amount': 100, 'currency': 'CNY'},
        contract_type='采购',
        rule_hints=['R-04 付款无验收前置条件'],
    )
    assert len(messages) == 2, '应为 system + user 两条消息'
    assert messages[0]['role'] == 'system'
    assert messages[1]['role'] == 'user'
    user_content = messages[1]['content']
    assert '条款正文' in user_content and '货到即付全款。' in user_content
    assert 'R-04 付款无验收前置条件' in user_content, '规则摘要必须喂给模型'
    assert '采购' in user_content, '合同类型必须带上'

    # 无规则命中时不应出现空列表报错
    messages = build_review_messages(
        clause={'content': 'x'}, meta={}, contract_type='*', rule_hints=None
    )
    assert '（无）' in messages[1]['content'], '无规则命中时应显示占位符'

    print('review_prompt 自测通过 ✅')
