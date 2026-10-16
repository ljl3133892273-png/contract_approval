'''processor/recommend_processor/prompt/rewrite_prompt.py'''

'''
P4 —— 条款改写提示词（SPEC 8.2）

输入: 原文 + 风险原因 + 模板库候选
输出: 建议条款文本 + 修改理由（JSON）

设计要点:
1. **输出 schema 与 P3 对齐**：统一走 `{"items": [...]}`。
   这样 mock 供应商返回的 `{"items": []}`（见 utils/llm_utils.MockLLMProvider）
   天然表示「没有产出改写」，节点就会自动退化为「直接用召回到的示范条款」，
   不需要为 P4 单独写一套 mock 分支 —— 降级路径与 SPEC 8.5 完全一致。

2. **立场锚定「我方 = 甲方」**：SPEC 6.2 定义 contract_meta.party_a 为我方，
   所以改写必须朝有利于甲方的方向调整，同时明确禁止模型把甲方写成乙方。

3. **禁止篡改关键事实**：合同金额、主体名称、日期、标的物这些是既定事实，
   模型只能改「权利义务的表述方式」，不能自己填数。这是防幻觉的关键约束。

4. **必须给出可落地的完整条款正文**，不能只说「建议增加验收条款」这种空话 ——
   因为产出要直接回填到 risk_item.suggestion_text 并展示给法务抄进合同。

5. 全部中文，建议正文不超过 800 字（落到 risk_item.suggestion_text 是 TEXT，
   但过长无法在风险卡片里阅读）。
'''

from typing import Any, Dict, List

# 建议来源（与节点内的 source 取值保持一致，仅用于提示词文档）
REQUIRED_ITEM_FIELDS = ('suggestion_text', 'reason')

SYSTEM_PROMPT = f'''你是一名资深企业法务，负责把存在法律风险的合同条款改写为**可直接采用的合规条款**。
请针对给定的【单条风险条款】，只输出严格的 JSON，不要输出任何解释性文字、不要使用 markdown 代码块。

输出格式：
{{
  "items": [
    {{
      "suggestion_text": "改写后的完整条款正文（可直接抄进合同）",
      "reason": "本次修改解决了什么风险、依据是什么",
      "template_used": "参考的示范条款标题（未参考则填空字符串）"
    }}
  ]
}}

硬性要求：
1. 必须返回 1 条改写结果；只有在完全无法改写时才返回 {{"items": []}}。
2. suggestion_text 必须是**完整的、可以直接替换原文的条款正文**，
   包含条款编号与标题（若原文有），不能只写"建议增加……"这类空泛说明。
3. **不得篡改既定事实**：合同金额、币种、签约主体名称、统一社会信用代码、
   履行日期等只能沿用输入中给出的值，绝对不允许自行编造数字或名称。
4. 我方立场为【甲方】，改写方向应有利于甲方：明确权利义务对等、增加必要的前置条件、
   限定责任上限与时限、把管辖权约定在甲方所在地等。
5. 不得把甲方写成乙方，也不得把风险转嫁给合同外第三方。
6. suggestion_text 不超过 800 字；全部使用中文书面语。
7. 只输出 JSON 本体，不要包裹代码块围栏。'''


def build_user_prompt(risk_item: Dict[str, Any], meta: Dict[str, Any],
                      contract_type: str,
                      original_content: str,
                      templates: List[Dict[str, Any]] = None) -> str:
    """
    构建 P4 的用户消息

    :param risk_item: 风险项（含 dimension / risk_type / risk_level / cause / legal_basis）
    :param meta: 合同元数据（party_a / party_b / amount / currency）
    :param contract_type: 合同类型
    :param original_content: 风险条款原文（文档级风险时为空）
    :param templates: 召回到的候选示范条款，按得分降序（可为空）
    :return: 用户提示词文本
    """
    templates = templates or []

    amount = meta.get('amount')
    amount_text = f'{amount} {meta.get("currency") or ""}'.strip() if amount else '未提取到'

    if templates:
        template_text = '\n\n'.join(
            f'【候选 {index + 1}】{item.get("title") or "(无标题)"}'
            f'（合同类型 {item.get("contract_type") or "*"} / 条款类型 {item.get("clause_type") or "other"}）\n'
            f'{item.get("content") or ""}'
            for index, item in enumerate(templates)
        )
    else:
        template_text = '（模板库未召回到候选，请基于法律常识与原文自行改写）'

    original_text = original_content.strip() or '（本风险点属于整份合同层面的问题，没有对应的单条条款原文）'

    return f'''【合同基本信息】
合同类型：{contract_type}
我方（甲方）：{meta.get('party_a') or '未提取到'}
相对方（乙方）：{meta.get('party_b') or '未提取到'}
合同金额：{amount_text}

【需要解决的风险点】
风险维度：{risk_item.get('dimension') or '未标注'}
风险类型：{risk_item.get('risk_type') or '未标注'}
风险等级：{risk_item.get('risk_level') or '未标注'}
风险成因：{risk_item.get('cause') or '未提供'}
合规依据：{risk_item.get('legal_basis') or '未提供'}

【原条款正文】
{original_text}

【模板库候选示范条款（供参考，可组合可调整）】
{template_text}

请输出改写后的 JSON。'''


def build_rewrite_messages(risk_item: Dict[str, Any], meta: Dict[str, Any],
                           contract_type: str, original_content: str,
                           templates: List[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """
    构建完整的 P4 对话消息（system + user）
    :return: [{'role': 'system', 'content': ...}, {'role': 'user', 'content': ...}]
    """
    return [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {
            'role': 'user',
            'content': build_user_prompt(
                risk_item, meta, contract_type, original_content, templates
            ),
        },
    ]


if __name__ == '__main__':
    # 轻量自测：提示词必须包含输出 schema 与关键硬约束
    assert 'items' in SYSTEM_PROMPT, '系统提示词必须声明输出 schema'
    for field_name in REQUIRED_ITEM_FIELDS:
        assert field_name in SYSTEM_PROMPT, f'系统提示词缺少输出字段 {field_name}'
    assert '不得篡改既定事实' in SYSTEM_PROMPT, '必须有防幻觉硬约束'
    assert '甲方' in SYSTEM_PROMPT, '必须锚定我方立场'

    risk = {
        'dimension': 'payment', 'risk_type': 'timing', 'risk_level': 'high',
        'cause': '付款未设验收前置条件', 'legal_basis': '《民法典》第626条',
    }
    templates = [{
        'title': '付款与验收前置条款（示范）', 'contract_type': '*',
        'clause_type': 'payment', 'content': '甲方应在验收合格后 15 个工作日内付款。',
    }]

    messages = build_rewrite_messages(
        risk, {'party_a': '甲公司', 'party_b': '乙公司', 'amount': 100, 'currency': 'CNY'},
        '采购', '货到即付全款。', templates,
    )
    assert len(messages) == 2, '应为 system + user 两条消息'
    assert messages[0]['role'] == 'system' and messages[1]['role'] == 'user'

    user_content = messages[1]['content']
    assert '货到即付全款。' in user_content, '原文必须带上'
    assert '付款未设验收前置条件' in user_content, '风险成因必须带上'
    assert '付款与验收前置条款（示范）' in user_content, '候选模板必须带上'
    assert '采购' in user_content and '甲公司' in user_content, '合同信息必须带上'

    # 无候选模板 / 无原文（文档级风险）都不应报错，要有明确的占位提示
    messages = build_rewrite_messages(
        {'cause': '相对方主体缺失'}, {}, '*', '', templates=None
    )
    fallback_content = messages[1]['content']
    assert '未召回到候选' in fallback_content, '无候选时应有占位提示'
    assert '没有对应的单条条款原文' in fallback_content, '文档级风险应有占位提示'

    print('rewrite_prompt 自测通过 ✅')
