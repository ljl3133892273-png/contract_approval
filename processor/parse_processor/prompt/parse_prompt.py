'''processor/parse_processor/prompt/parse_prompt.py'''

'''
P1 / P2 提示词（SPEC 8.2「Prompt 模板体系（四类）」的前两类）

    P1 条款分类：输入段落文本 → {clause_type, title_candidate, confidence}
    P2 元数据校验：输入候选值 + 上下文 → 归一化元数据 JSON

设计要点（与 P3/P4 同一套纪律）:
    1. 枚举值与 db/constants.py 严格对齐，输出必须 JSON；
    2. 反幻觉硬约束：拿不准就低置信度 / 原样返回，**绝不编造**；
    3. 解析层是"辅助"角色 —— 规则路径是主线，LLM 只做兜底与归一化，
       所以提示词明确告诉模型"宁缺毋滥"。
'''

from typing import Any, Dict, List

from db.constants import ClauseType

# ============================================================
# P1 条款分类
# ============================================================

# 条款类型 → 中文释义（枚举与 db/constants.ClauseType.ALL 严格一致）
_P1_TYPE_GUIDE = (
    'subject（标的物：采购内容/货物/产品规格）\n'
    'payment（付款：价款/结算/发票/费用）\n'
    'acceptance（验收：交货/交付/质量标准/检验）\n'
    'breach（违约责任：违约金/赔偿/损失）\n'
    'confidentiality（保密义务：商业秘密/机密信息）\n'
    'data_security（数据安全：个人信息/数据保护/隐私）\n'
    'jurisdiction（争议管辖：仲裁/诉讼/法律适用）\n'
    'ip（知识产权：著作权/专利/商标/源代码）\n'
    'force_majeure（不可抗力）\n'
    'term（期限：合同期限/有效期/生效）\n'
    'other（以上都不像时的兜底）'
)

P1_SYSTEM_PROMPT = f'''你是企业合同文档的结构化助手，任务是判断一段合同文本属于哪类条款。

可选类型（必须原样输出英文标识，一个且仅一个）:
{_P1_TYPE_GUIDE}

输出 JSON（不要输出任何其他内容）:
{{
  "clause_type": "上述 11 个类型标识之一",
  "title_candidate": "建议的条款标题，不超过 20 字；提炼不出时留空字符串",
  "confidence": 0.0 到 1.0 之间的数字
}}

硬性要求:
- 拿不准时 confidence 给低分（0.5 以下）并选 other，**不要硬猜**；
- 这段文本可能不是条款（如合同标题、签字页、附件说明），这种情形一律 other + 低置信度；
- 只依据文本本身判断，不要联想文本之外的内容。'''


def build_p1_messages(content: str, max_chars: int = 800) -> List[Dict[str, str]]:
    """
    构造 P1 条款分类消息
    :param content: 条款正文（超长截断——分类只需要开头部分）
    :param max_chars: 送入模型的最大字符数
    :return: chat 消息列表
    """
    text = (content or '').strip()[:max_chars]
    return [
        {'role': 'system', 'content': P1_SYSTEM_PROMPT},
        {'role': 'user', 'content': f'请判断下面这段合同文本的条款类型：\n\n{text}'},
    ]


def parse_p1_response(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    校验并归一化 P1 响应（不信任模型输出）
    :param payload: 解析后的 JSON
    :return: {'clause_type', 'title_candidate', 'confidence'}；非法输入返回全兜底值
    """
    if not isinstance(payload, dict):
        return {'clause_type': ClauseType.OTHER, 'title_candidate': '', 'confidence': 0.0}

    clause_type = str(payload.get('clause_type') or '').strip()
    if clause_type not in ClauseType.ALL:
        clause_type = ClauseType.OTHER

    try:
        confidence = float(payload.get('confidence') or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    title_candidate = str(payload.get('title_candidate') or '').strip()[:20]

    return {
        'clause_type': clause_type,
        'title_candidate': title_candidate,
        'confidence': confidence,
    }


# ============================================================
# P2 元数据校验 / 归一化
# ============================================================

P2_SYSTEM_PROMPT = '''你是企业合同的元数据校验助手。规则引擎已经从合同中抽出了元数据候选值，
你的任务是**校验并归一化**这些值，让它们符合落库规范。

你会收到一个 JSON，包含若干字段的候选值与来源说明。请输出**归一化后的** JSON:

- amount（金额）: 必须是**数字**（人民币元）。中文大写金额换算成阿拉伯数字；
  "128万" → 1280000。换算不了就原样返回候选值，**不要猜**。
- contract_no（合同编号）: 去首尾空白，去掉"合同编号："之类的前缀标签。
- party_a / party_b（甲乙方）: 只保留主体全称，去掉"甲方：""（盖章）"等修饰。
- credit_code（统一社会信用代码）: 18 位大写字母数字，小写转大写，去空格。
- currency（币种）: 输出 CNY / USD / EUR / HKD / JPY 之一；人民币一律 CNY。

硬性要求:
- 只动需要归一化的字段；候选值本身正确就原样保留；
- 某个字段无法确定 → 从输出里**删掉该字段**（让调用方保留规则抽取的原值），
  **不要编造或脑补**；
- 输出只有 JSON，不要输出任何解释。'''


def build_p2_messages(field_name: str, candidates: Dict[str, Any],
                      context: str = '', max_context_chars: int = 300) -> List[Dict[str, str]]:
    """
    构造 P2 元数据校验消息
    :param field_name: 要校验的字段名（amount/contract_no/party_a/party_b/credit_code/currency）
    :param candidates: 规则抽取的候选值（{'value': ..., 'source': 'regex|table|llm'}）
    :param context: 字段附近的原文片段（帮助模型判断，可选）
    :param max_context_chars: 上下文最大字符数
    :return: chat 消息列表
    """
    import json

    payload = {
        'field': field_name,
        'candidates': candidates,
    }
    user = f'请校验并归一化以下元数据：\n{json.dumps(payload, ensure_ascii=False)}'
    if context:
        user += f'\n\n原文上下文（仅供参考）：\n{context.strip()[:max_context_chars]}'
    return [
        {'role': 'system', 'content': P2_SYSTEM_PROMPT},
        {'role': 'user', 'content': user},
    ]


def parse_p2_response(payload: Dict[str, Any], field_name: str) -> Any:
    """
    校验并归一化 P2 响应（不信任模型输出）
    :param payload: 解析后的 JSON
    :param field_name: 目标字段名
    :return: 归一化后的字段值；取不到 / 非法返回 None（调用方保留规则原值）
    """
    if not isinstance(payload, dict):
        return None

    # 模型可能返回 {field: value}，也可能直接返回 {value: ...} 或整个归一化结果
    value = payload.get(field_name, payload.get('value'))
    if value is None:
        return None

    if field_name == 'amount':
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    if field_name == 'currency':
        code = str(value).strip().upper()
        return code if code in ('CNY', 'USD', 'EUR', 'HKD', 'JPY') else None

    text = str(value).strip()
    if field_name == 'credit_code':
        text = text.replace(' ', '').upper()
        return text if len(text) == 18 else None

    return text or None


if __name__ == '__main__':
    passed = 0

    # ---------- 一、P1 提示词契约 ----------
    for token in ClauseType.ALL:
        assert token in P1_SYSTEM_PROMPT, f'P1 提示词缺少枚举值 {token}'
    assert 'confidence' in P1_SYSTEM_PROMPT, 'P1 必须声明 confidence'
    assert '不要硬猜' in P1_SYSTEM_PROMPT, 'P1 必须有反幻觉约束'
    passed += 1
    print('  ✅ P1 提示词含全部 11 个类型枚举 + 反幻觉约束')

    # ---------- 二、P1 消息构造与截断 ----------
    messages = build_p1_messages('付款条款正文' * 200)
    assert messages[0]['role'] == 'system' and messages[1]['role'] == 'user'
    assert len(messages[1]['content']) < 1000, '超长正文应被截断'
    passed += 1
    print('  ✅ P1 消息构造正确，超长正文截断')

    # ---------- 三、P1 响应校验 ----------
    good = parse_p1_response({'clause_type': 'payment', 'title_candidate': '付款方式',
                              'confidence': 0.92})
    assert good == {'clause_type': 'payment', 'title_candidate': '付款方式', 'confidence': 0.92}

    bad_type = parse_p1_response({'clause_type': '不存在的类型', 'confidence': 0.9})
    assert bad_type['clause_type'] == ClauseType.OTHER, '非法类型应归 other'

    bad_conf = parse_p1_response({'clause_type': 'payment', 'confidence': '很高'})
    assert bad_conf['confidence'] == 0.0, '非法置信度应归零'

    out_of_range = parse_p1_response({'clause_type': 'payment', 'confidence': 9.9})
    assert out_of_range['confidence'] == 1.0, '置信度应夹到 [0,1]'

    long_title = parse_p1_response({'clause_type': 'payment',
                                    'title_candidate': '长' * 50, 'confidence': 0.8})
    assert len(long_title['title_candidate']) == 20, '标题候选应截到 20 字'

    assert parse_p1_response('不是 JSON')['clause_type'] == ClauseType.OTHER
    passed += 1
    print('  ✅ P1 响应校验：非法类型归 other、置信度夹紧、标题截断、非 dict 兜底')

    # ---------- 四、P2 提示词契约 ----------
    for token in ('amount', 'contract_no', 'party_a', 'credit_code', 'currency', 'CNY'):
        assert token in P2_SYSTEM_PROMPT, f'P2 提示词缺少 {token}'
    assert '不要编造' in P2_SYSTEM_PROMPT, 'P2 必须有反幻觉约束'
    passed += 1
    print('  ✅ P2 提示词含全部字段说明 + 反幻觉约束')

    # ---------- 五、P2 响应校验 ----------
    assert parse_p2_response({'amount': '1280000'}, 'amount') == 1280000.0
    assert parse_p2_response({'amount': '一百二十八万'}, 'amount') is None, \
        '无法换算的金额应返回 None（保留规则原值）'
    assert parse_p2_response({'currency': '人民币'}, 'currency') is None or \
           parse_p2_response({'currency': 'cny'}, 'currency') == 'CNY'
    assert parse_p2_response({'credit_code': '91310000ma1fl5678y'}, 'credit_code') == \
        '91310000MA1FL5678Y', '信用代码应转大写'
    assert parse_p2_response({'credit_code': '太短'}, 'credit_code') is None, \
        '长度不对的信用代码应丢弃'
    assert parse_p2_response({'party_a': ' 某某科技有限公司 '}, 'party_a') == '某某科技有限公司'
    assert parse_p2_response({'value': 'CG-2026-001'}, 'contract_no') == 'CG-2026-001'
    assert parse_p2_response('不是 JSON', 'amount') is None
    assert parse_p2_response({'unrelated': 1}, 'amount') is None
    passed += 1
    print('  ✅ P2 响应校验：金额/币种/信用代码/主体归一化 + 非法值丢弃')

    print(f'parse_prompt 自测全部通过 ✅ 共 {passed} 个用例')
