'''utils/llm_utils.py'''

"""
LLM 调用工具层

职责:
    1. 定义统一的 Provider 协议（SPEC 8.1）
    2. 提供两个实现：MockLLMProvider（降级用）与 OpenAICompatProvider（真实调用）
    3. 统一超时与重试策略（SPEC 8.4：超时 60s，重试 2 次，指数退避 1s / 4s）
    4. 统一 JSON 结构化输出与解析兜底（SPEC 8.2）

关于同步 / 异步:
    SPEC 8.1 给的协议是 async，但本项目 LangGraph 的节点方法是**同步**的
    （`process(self, state)`），在同步节点里套 asyncio 只会徒增复杂度。
    所以这里以 `chat()` 同步为主接口，同时提供 `achat()` 异步包装，两种姿势都支持。

关于 mock（用户已确认「真实接入 + mock 开关」）:
    .env 里的 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 尚未填写，
    因此当前 `get_llm_provider()` 返回 MockLLMProvider。
    mock 的行为是「返回结构合法的**空**结果」，而不是伪造风险结论 ——
    这样任务仍能跑完（只剩规则引擎产出的风险项），与 SPEC 8.5 的降级策略完全一致。
    把 LLM_ALLOW_MOCK 设为 false，则未配置时直接抛错进入 blocked，绝不产出假数据。

异常说明:
    本模块抛 `LLMProviderError`（基础设施层异常），
    与各 processor 自己 exceptions.py 里的 `LLMError`（业务层异常）区分开 ——
    节点捕获 LLMProviderError 后，按业务语义包装成自己的 LLMError 或走降级。
"""

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional, Protocol

from config_bag.llm_config import LLMConfig, get_llm_config
from utils.json_format_utils import safe_json_loads

logger = logging.getLogger(__name__)

# 对话消息类型：形如 [{'role': 'system', 'content': '...'}]
ChatMessage = Dict[str, str]


class LLMProviderError(Exception):
    """LLM 供应商调用异常（基础设施层，跨 processor 共用）"""

    def __init__(self, message: str, cause: Exception = None):
        self.cause = cause
        super().__init__(message)

    def __str__(self):
        base = super().__str__()
        if self.cause:
            return f'{base}（原因: {self.cause}）'
        return base


class LLMProvider(Protocol):
    """
    LLM 供应商统一接口（SPEC 8.1）

    实现类：
    - MockLLMProvider       未配置 / 强制降级时使用
    - OpenAICompatProvider  DeepSeek / 通义 / 智谱 / 本地 vLLM 等 OpenAI 兼容端点
    """

    name: str  # 供应商名称
    is_mock: bool  # 是否为 mock 实现

    def chat(self, messages: List[ChatMessage], temperature: Optional[float] = None,
             json_mode: bool = False) -> str:
        """同步对话，返回模型输出文本"""
        ...

    async def achat(self, messages: List[ChatMessage], temperature: Optional[float] = None,
                    json_mode: bool = False) -> str:
        """异步对话，返回模型输出文本"""
        ...

    def chat_stream(self, messages: List[ChatMessage],
                    temperature: Optional[float] = None) -> Iterator[str]:
        """流式对话，逐段产出文本增量"""
        ...


class MockLLMProvider:
    """
    mock 供应商：未配置 LLM 时的降级实现

    关键约定:
        json_mode 下返回**结构合法的空结果** `{"_mock": true, "_reason": "...", "items": []}`。
        各 processor 的 Prompt（P1~P4）统一从 `items` 取结果，因此 mock 时：
        - 条款分类拿不到结果 → 归 other（与 SPEC「置信度低则归入 other」一致）
        - 审查拿不到风险项   → 任务仍可完成，只保留规则引擎结果（SPEC 8.5）
        绝不编造风险结论，避免把假数据当真结果送进审批流。
    """

    name = 'mock'
    is_mock = True

    def __init__(self, reason: str = 'LLM 未配置'):
        self.reason = reason
        self._warned = False

    def _warn_once(self) -> None:
        """只在首次调用时告警一次，避免刷屏"""
        if not self._warned:
            logger.warning(
                f'[LLM] 正在使用 mock 供应商（{self.reason}）—— '
                f'链路可照常跑通，但产出的是空结果，不可用于真实审查'
            )
            self._warned = True

    def chat(self, messages: List[ChatMessage], temperature: Optional[float] = None,
             json_mode: bool = False) -> str:
        """返回结构合法的空结果"""
        self._warn_once()
        if json_mode:
            return json.dumps(
                {'_mock': True, '_reason': self.reason, 'items': []},
                ensure_ascii=False
            )
        return f'[LLM-MOCK] {self.reason}'

    async def achat(self, messages: List[ChatMessage], temperature: Optional[float] = None,
                    json_mode: bool = False) -> str:
        """异步版本：无 IO，直接返回"""
        return self.chat(messages, temperature=temperature, json_mode=json_mode)

    def chat_stream(self, messages: List[ChatMessage],
                    temperature: Optional[float] = None) -> Iterator[str]:
        """流式版本：一次性吐出全部内容"""
        yield self.chat(messages, temperature=temperature)


class OpenAICompatProvider:
    """
    OpenAI 兼容供应商实现

    适用于 DeepSeek / 通义千问（DashScope 兼容模式）/ 智谱 / 本地 vLLM 等
    任何暴露 OpenAI 兼容接口的服务。
    """

    name = 'openai_compat'
    is_mock = False

    # 重试退避秒数（SPEC 8.4：指数退避 1s / 4s）。
    # 定义成类属性是为了便于测试时置 0，避免测试真的睡 5 秒。
    _RETRY_BACKOFF = (1, 4)

    def __init__(self, config: LLMConfig):
        self.config = config
        self._client = None

    def _get_client(self):
        """
        【辅助函数】惰性创建 OpenAI 客户端
        功能：把 SDK 的引入推迟到真正调用时，这样没装 openai 也不影响其余功能（走 mock）
        """
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as e:
                raise LLMProviderError(
                    message='未安装 openai SDK，请执行：uv add openai',
                    cause=e
                )
            self._client = OpenAI(
                base_url=self.config.base_url,
                api_key=self.config.api_key,
                timeout=self.config.timeout,
            )
        return self._client

    def _call_once(self, messages: List[ChatMessage], temperature: Optional[float],
                   json_mode: bool) -> str:
        """
        【辅助函数】单次调用（不含重试），独立出来便于测试重试逻辑
        """
        client = self._get_client()

        kwargs: Dict[str, Any] = {
            'model': self.config.model,
            'messages': messages,
            'temperature': self.config.temperature if temperature is None else temperature,
        }
        if json_mode:
            # 供应商不一定都支持 response_format，不支持时会抛错并进入重试
            kwargs['response_format'] = {'type': 'json_object'}

        response = client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content
        return content or ''

    def chat(self, messages: List[ChatMessage], temperature: Optional[float] = None,
             json_mode: bool = False) -> str:
        """
        同步对话

        重试策略（SPEC 8.4）：单次失败后按 1s / 4s 退避重试，共尝试 max_retries + 1 次。
        全部失败抛 LLMProviderError，由调用方决定降级还是进入 blocked。
        """
        attempts = self.config.max_retries + 1
        last_error: Optional[Exception] = None

        for attempt in range(attempts):
            try:
                return self._call_once(messages, temperature, json_mode)
            except Exception as e:
                last_error = e
                if attempt < attempts - 1:
                    delay = self._RETRY_BACKOFF[min(attempt, len(self._RETRY_BACKOFF) - 1)]
                    logger.warning(
                        f'[LLM] 第 {attempt + 1}/{attempts} 次调用失败：{e}，'
                        f'{delay}s 后重试'
                    )
                    if delay:
                        time.sleep(delay)

        raise LLMProviderError(
            message=f'LLM 连续 {attempts} 次调用失败（模型 {self.config.model}）',
            cause=last_error
        )

    async def achat(self, messages: List[ChatMessage], temperature: Optional[float] = None,
                    json_mode: bool = False) -> str:
        """
        异步对话
        用 to_thread 把同步实现搬出事件循环：无需再维护一套异步 SDK 逻辑，
        且不会阻塞事件循环里的其它协程。
        """
        return await asyncio.to_thread(self.chat, messages, temperature, json_mode)

    def chat_stream(self, messages: List[ChatMessage],
                    temperature: Optional[float] = None) -> Iterator[str]:
        """流式对话，逐段产出文本增量"""
        client = self._get_client()
        stream = client.chat.completions.create(
            model=self.config.model,
            messages=messages,
            temperature=self.config.temperature if temperature is None else temperature,
            stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            content = getattr(delta, 'content', None)
            if content:
                yield content


# ==================== 全局单例 ====================
_provider: Optional[LLMProvider] = None


def _build_provider(force_mock: bool = False,
                    config: Optional[LLMConfig] = None) -> LLMProvider:
    """
    【辅助函数】按配置决定使用哪个 Provider
    :param force_mock: 调用方强制要求 mock
    :param config: 配置对象，默认取全局单例（便于测试注入）
    :return: Provider 实例
    """
    config = config or get_llm_config()

    # 1.调用方强制 mock
    if force_mock:
        return MockLLMProvider(reason='调用方强制使用 mock')

    # 2.配置齐全 → 真实调用
    if config.is_configured:
        logger.info(
            f'[LLM] 使用 {config.base_url} / {config.model} '
            f'(key={config.masked_api_key})'
        )
        return OpenAICompatProvider(config)

    # 3.未配置 → 按开关决定降级还是报错
    if config.allow_mock:
        return MockLLMProvider(reason=f'.env 未配置 {config.missing_names}')

    raise LLMProviderError(
        message=f'LLM 未配置（缺少 {config.missing_names}），'
                f'且 LLM_ALLOW_MOCK=false，无法执行 LLM 调用'
    )


def get_llm_provider(force_mock: bool = False) -> LLMProvider:
    """
    获取 LLM Provider
    :param force_mock: 强制使用 mock（不走缓存）
    :return: Provider 实例
    """
    global _provider
    if force_mock:
        return _build_provider(force_mock=True)
    if _provider is None:
        _provider = _build_provider()
    return _provider


def reset_llm_provider() -> None:
    """清空 Provider 单例（配置变更或测试时使用）"""
    global _provider
    _provider = None


def chat_json(messages: List[ChatMessage], temperature: Optional[float] = None,
              force_mock: bool = False) -> Any:
    """
    调用 LLM 并要求 JSON 输出，直接返回解析后的对象。

    解析失败**不抛异常**，返回 None，由调用方按 SPEC 8.5 降级
    （例如审查节点拿不到 LLM 风险项时，仍保留规则引擎结果并把任务跑完）。
    :param messages: 对话消息
    :param temperature: 温度，None 表示用配置默认值
    :param force_mock: 强制 mock
    :return: 解析后的 JSON 对象；解析失败返回 None
    """
    provider = get_llm_provider(force_mock)
    raw = provider.chat(messages, temperature=temperature, json_mode=True)
    return safe_json_loads(raw, default=None)


if __name__ == '__main__':
    # 激活日志
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    passed = 0

    # ---------- 一、未配置 → 走 mock ----------
    logging.getLogger().info('=' * 22 + ' 一、未配置走 mock ' + '=' * 22)

    reset_llm_provider()
    provider = get_llm_provider()
    config = get_llm_config()

    if config.is_configured:
        logging.getLogger().info('  ⏭ .env 已配置 LLM，跳过 mock 分支用例')
        assert provider.is_mock is False, '已配置时应使用真实 Provider'
        passed += 1
    else:
        assert provider.is_mock is True, '未配置时应降级为 mock'
        assert provider.name == 'mock'

        # mock 的 JSON 输出必须是「结构合法的空结果」，不能编造风险结论
        raw = provider.chat([{'role': 'user', 'content': 'test'}], json_mode=True)
        payload = json.loads(raw)
        assert payload['_mock'] is True, 'mock 结果必须带 _mock 标记'
        assert payload['items'] == [], 'mock 结果必须是空的，不得编造内容'
        # 非 JSON 模式
        assert '[LLM-MOCK]' in provider.chat([{'role': 'user', 'content': 'test'}]), \
            'mock 文本应带明显标记'
        passed += 1
        logging.getLogger().info('  ✅ 未配置 → mock，输出为带标记的空结果')

    # ---------- 二、chat_json 解析 ----------
    logging.getLogger().info('=' * 22 + ' 二、chat_json ' + '=' * 22)

    result = chat_json([{'role': 'user', 'content': 'test'}], force_mock=True)
    assert isinstance(result, dict), 'chat_json 应返回字典'
    assert result.get('_mock') is True, 'mock 下应带 _mock 标记'
    passed += 1
    logging.getLogger().info('  ✅ chat_json 正确解析为字典')

    # ---------- 三、强制 mock ----------
    logging.getLogger().info('=' * 22 + ' 三、force_mock ' + '=' * 22)

    assert get_llm_provider(force_mock=True).is_mock is True, 'force_mock 应返回 mock'
    passed += 1
    logging.getLogger().info('  ✅ force_mock=True 强制走 mock')

    # ---------- 四、allow_mock=false 时不得产出假数据 ----------
    logging.getLogger().info('=' * 22 + ' 四、mock 关闭 ' + '=' * 22)

    strict_config = LLMConfig(base_url='', api_key='', model='', allow_mock=False)
    try:
        _build_provider(force_mock=False, config=strict_config)
    except LLMProviderError as e:
        assert 'LLM_ALLOW_MOCK' in str(e), '错误信息应指明开关名'
        passed += 1
        logging.getLogger().info('  ✅ allow_mock=false → 正确抛出 LLMProviderError')
    else:
        raise AssertionError('allow_mock=false 且未配置时不应返回 Provider')

    # ---------- 五、重试策略（不联网，直接替换单次调用）----------
    logging.getLogger().info('=' * 22 + ' 五、重试策略 ' + '=' * 22)

    retry_config = LLMConfig(base_url='http://127.0.0.1:1', api_key='k', model='m', max_retries=2)

    # 5.1 一直失败 → 尝试 3 次后抛 LLMProviderError
    always_fail_provider = OpenAICompatProvider(retry_config)
    always_fail_provider._RETRY_BACKOFF = (0, 0)  # 不真的 sleep
    fail_calls = {'n': 0}

    def _always_fail(*args, **kwargs):
        fail_calls['n'] += 1
        raise RuntimeError('模拟接口异常')

    always_fail_provider._call_once = _always_fail
    try:
        always_fail_provider.chat([{'role': 'user', 'content': 'hi'}])
    except LLMProviderError as e:
        assert fail_calls['n'] == 3, f'应尝试 max_retries+1=3 次，实际 {fail_calls["n"]}'
        assert isinstance(e.cause, RuntimeError), '应保留底层原始异常'
        passed += 1
        logging.getLogger().info('  ✅ 连续失败 → 尝试 3 次后抛 LLMProviderError（保留 cause）')
    else:
        raise AssertionError('一直失败时不应返回结果')

    # 5.2 第一次失败、第二次成功 → 自动重试成功
    flaky_provider = OpenAICompatProvider(retry_config)
    flaky_provider._RETRY_BACKOFF = (0, 0)
    flaky_calls = {'n': 0}

    def _flaky(*args, **kwargs):
        flaky_calls['n'] += 1
        if flaky_calls['n'] == 1:
            raise RuntimeError('第一次失败')
        return '{"ok": true}'

    flaky_provider._call_once = _flaky
    assert flaky_provider.chat([]) == '{"ok": true}', '重试后应返回成功结果'
    assert flaky_calls['n'] == 2, f'应调用 2 次，实际 {flaky_calls["n"]}'
    passed += 1
    logging.getLogger().info('  ✅ 首次失败 → 自动重试一次后成功')

    # ---------- 六、异步接口 ----------
    logging.getLogger().info('=' * 22 + ' 六、异步接口 ' + '=' * 22)

    async def _run_async_case():
        mock_provider = MockLLMProvider(reason='测试')
        return await mock_provider.achat([{'role': 'user', 'content': 'test'}], json_mode=True)

    async_result = asyncio.run(_run_async_case())
    assert json.loads(async_result)['_mock'] is True, 'achat 应可用'
    passed += 1
    logging.getLogger().info('  ✅ achat 异步接口可用')

    logging.getLogger().info(f'llm_utils 自测全部通过 ✅ 共 {passed} 个用例')
