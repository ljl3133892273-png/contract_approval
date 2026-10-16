'''utils/json_format_utils.py'''

"""
JSON 解析兜底工具

为什么需要这一层:
    LLM 返回的「JSON」往往不干净 —— 外面裹着 ```json 围栏，或者前后带一句客套话
    （「好的，以下是分析结果：{...}」）。直接 json.loads 会解析失败。
    SPEC 8.2 明确要求「做本地 JSON 解析兜底（剔除 markdown 代码块围栏）」，
    本模块就是这个兜底层，被所有 LLM 调用方复用。

使用建议:
    解析失败**不要抛异常**，用 safe_json_loads 拿到 None 后由调用方降级
    （SPEC 8.5：LLM 失败时任务仍可完成，只保留规则引擎结果）。
"""

import json
import re
from typing import Any, Optional

# markdown 代码块围栏：```json ... ``` / ``` ... ```
_JSON_FENCE_PATTERN = re.compile(r'```(?:json|JSON)?\s*(.*?)```', re.DOTALL)


def strip_json_fence(text: str) -> str:
    """
    剔除 markdown 代码块围栏，只留里面的内容。
    没有围栏时原样返回（并做去空白）。
    :param text: 原始文本
    :return: 去掉围栏后的文本
    """
    if not text:
        return ''
    match = _JSON_FENCE_PATTERN.search(text)
    if match and match.group(1).strip():
        return match.group(1).strip()
    return text.strip()


def extract_json_text(text: str) -> str:
    """
    从一堆文本里抠出第一个「括号配对完整」的 JSON 对象或数组。
    用于应对前后夹带自然语言的场景。
    :param text: 原始文本
    :return: JSON 文本；没找到返回空字符串
    """
    if not text:
        return ''

    # 找第一个 { 或 [ 作为起点
    start = -1
    for index, char in enumerate(text):
        if char in '{[':
            start = index
            break

    if start == -1:
        return ''

    return _match_balanced_brackets(text, start)


def _match_balanced_brackets(text: str, start: int) -> str:
    """
    【辅助函数】从 start 处的括号开始，找到配对的结束括号。
    需要正确处理字符串内部的括号与转义字符，否则会被 `{"a": "}"}` 这类内容骗到。
    :param text: 原始文本
    :param start: 起始括号的位置
    :return: 完整 JSON 片段；未配对返回空字符串
    """
    opening = text[start]
    closing = '}' if opening == '{' else ']'

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start:index + 1]

    return ''


def safe_json_loads(text: str, default: Optional[Any] = None) -> Any:
    """
    尽力而为地解析 JSON。
    依次尝试：原样 → 去围栏 → 抠出括号片段。全部失败则返回 default，**不抛异常**。
    :param text: 原始文本（通常是 LLM 的返回内容）
    :param default: 解析失败时的返回值
    :return: 解析结果或 default
    """
    if not text or not text.strip():
        return default

    candidates = (text.strip(), strip_json_fence(text), extract_json_text(strip_json_fence(text)))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

    return default


if __name__ == '__main__':
    # 轻量自测
    # 1. 纯粹的 JSON
    assert safe_json_loads('{"a": 1}') == {'a': 1}
    # 2. 带 ```json 围栏
    assert safe_json_loads('```json\n{"a": 1}\n```') == {'a': 1}
    # 3. 带 ``` 围栏但没写语言
    assert safe_json_loads('```\n[1, 2, 3]\n```') == [1, 2, 3]
    # 4. 前后夹带自然语言
    assert safe_json_loads('好的，以下是分析结果：{"a": 1}。希望对你有帮助。') == {'a': 1}
    # 5. 字符串里含花括号（必须按字符串规则跳过，否则会提前截断）
    assert safe_json_loads('{"a": "}"}') == {'a': '}'}
    assert safe_json_loads('{"a": "{\\"nested\\": 1}"}') == {'a': '{"nested": 1}'}
    # 6. 数组
    assert safe_json_loads('分析如下：\n[{"x": 1}, {"x": 2}]') == [{'x': 1}, {'x': 2}]
    # 7. 解析不了 → 返回 default，不抛异常
    assert safe_json_loads('完全不是 JSON') is None
    assert safe_json_loads('完全不是 JSON', default={}) == {}
    assert safe_json_loads('') is None
    assert safe_json_loads(None) is None
    # 8. 括号不配对
    assert safe_json_loads('{"a": 1') is None
    # 9. strip_json_fence 单独行为
    assert strip_json_fence('```json\n{"a":1}\n```') == '{"a":1}'
    assert strip_json_fence('{"a":1}') == '{"a":1}'

    print('json_format_utils 自测通过 ✅')
