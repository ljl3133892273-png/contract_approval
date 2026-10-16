'''processor/writeback_processor/adapter/approve_adapter.py'''

'''
审批系统 Adapter（SPEC 4.5）

接口（与本地 mock 服务 mock_approve_service/main.py 一一对应）:
    POST {base}/docs/{doc_id}/comments    写回评论
    GET  {base}/docs/{doc_id}             查单据（回写前校验单据存在）
    GET  {base}/docs/{doc_id}/attachment  下载附件（供解析子图的审批来源使用）

关于重试的职责划分（重要）:
    本 Adapter **不做重试** —— 它只负责"发一次，然后如实报告成功/失败"。
    重试与退避由 `node_adapter_send` 统一控制，原因是：
      1. 重试策略是可配置项（SPEC 4.5：重试 2 次，退避 5s / 30s），应该只有一处实现；
      2. 每次尝试都要记进 `review_comment.attempt_count` 与审计日志，放在节点里才拿得到；
      3. 端到端验证时可以用 mock 服务的失败注入精确控制"第几次成功"，
         如果 Adapter 内部藏着重试，这个控制就失效了。

关于 `retryable`:
    4xx（除 408 / 429）表示"这个请求本身有问题"（单据不存在 / 正文非法 / 令牌错），
    重试一万次也不会成功，只会白等 5s + 30s 退避。所以标记为不可重试，
    让节点立刻落 failed，把真正的问题暴露出来。
'''

import logging
import secrets
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from processor.writeback_processor.config import WritebackConfig
from processor.writeback_processor.exceptions import AdapterError

logger = logging.getLogger(__name__)

# 不可重试的 HTTP 状态码白名单之外的 4xx 都不可重试
_RETRYABLE_STATUS = {408, 429}
# 响应体截断长度（只用于报错信息，避免把整篇 HTML 错误页塞进日志）
_BODY_SNIPPET = 300


class ApproveAdapter:
    """
    审批系统 HTTP Adapter

    `is_mock = False` —— 真实调用对端。
    """

    name = 'http'
    is_mock = False

    def __init__(self, config: Optional[WritebackConfig] = None):
        self.config = config or WritebackConfig.from_env()

    # ==================== 内部工具 ====================
    def _base(self) -> str:
        """基础地址（去掉末尾斜杠，避免拼出 //）"""
        return (self.config.approve_api_base or '').rstrip('/')

    def _headers(self) -> Dict[str, str]:
        headers = {
            'Content-Type': 'application/json; charset=utf-8',
            'Accept': 'application/json',
        }
        if self.config.approve_token:
            headers['Authorization'] = f'Bearer {self.config.approve_token}'
        return headers

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """
        发一次请求并把异常统一转成 AdapterError
        :param method: HTTP 方法
        :param url: 完整 URL
        :return: httpx.Response（已确认是 2xx）
        :raises AdapterError: 网络异常、超时、或非 2xx
        """
        timeout = self.config.approve_timeout
        try:
            response = httpx.request(
                method, url, headers=self._headers(), timeout=timeout, **kwargs
            )
        except httpx.TimeoutException as e:
            raise AdapterError(
                message=f'审批接口超时（{timeout}s）：{method} {url}',
                cause=e, retryable=True,
            )
        except httpx.HTTPError as e:
            # 连接失败 / DNS 失败 / 协议错误等
            raise AdapterError(
                message=f'审批接口不可达：{method} {url} —— {e}',
                cause=e, retryable=True,
            )

        if response.is_success:
            return response

        body = (response.text or '')[:_BODY_SNIPPET]
        status_code = response.status_code
        retryable = status_code >= 500 or status_code in _RETRYABLE_STATUS
        raise AdapterError(
            message=f'审批接口返回 {status_code}：{method} {url} —— {body}',
            retryable=retryable, status_code=status_code, response_body=body,
        )

    def _json(self, response: httpx.Response) -> Dict[str, Any]:
        """
        解析响应 JSON；对端返回非 JSON（如网关的 HTML 错误页）时不抛异常，返回空字典
        :param response: 响应对象
        :return: 解析后的字典；失败时为空字典
        """
        try:
            payload = response.json()
        except ValueError:
            logger.warning('[回写] 审批接口返回的不是合法 JSON，已忽略响应体')
            return {}
        return payload if isinstance(payload, dict) else {}

    # ==================== 业务接口 ====================
    def send_comment(self, doc_id: str, content_md: str, idempotency_key: str = '',
                     task_id: str = '', operator: str = 'system') -> Dict[str, Any]:
        """
        写回审查评论
        :param doc_id: 审批单号
        :param content_md: 评论 Markdown 正文
        :param idempotency_key: 幂等键（SPEC 4.5：task_id + 审批单号）
        :param task_id: 本地任务 ID（对端可留作排查线索）
        :param operator: 操作人
        :return: {'comment_id': str, 'duplicated': bool, 'raw': dict}
        :raises AdapterError: 调用失败
        """
        if not self.config.is_configured:
            raise AdapterError(
                message=f'审批系统未配置（缺少 {self.config.missing_names}），无法回写',
                retryable=False,
            )

        url = f'{self._base()}/docs/{doc_id}/comments'
        response = self._request('POST', url, json={
            'content_md': content_md,
            'idempotency_key': idempotency_key,
            'task_id': task_id,
            'operator': operator,
        })
        payload = self._json(response)
        data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
        return {
            'comment_id': str(data.get('comment_id') or ''),
            # 对端判定为重复提交 —— 说明这个幂等键之前已经成功写回过
            'duplicated': bool(data.get('duplicated')),
            'raw': payload,
        }

    def fetch_doc(self, doc_id: str) -> Dict[str, Any]:
        """
        查询审批单据详情
        :param doc_id: 审批单号
        :return: 单据字典
        :raises AdapterError: 调用失败（单据不存在时 status_code=404、retryable=False）
        """
        if not self.config.is_configured:
            raise AdapterError(
                message=f'审批系统未配置（缺少 {self.config.missing_names}），无法查询单据',
                retryable=False,
            )

        url = f'{self._base()}/docs/{doc_id}'
        payload = self._json(self._request('GET', url))
        data = payload.get('data')
        return data if isinstance(data, dict) else {}

    def download_attachment(self, doc_id: str, dest_path: str) -> str:
        """
        下载审批单据的附件到本地（对应 SPEC 4.1.1 的 approve_push / approve_pull）
        :param doc_id: 审批单号
        :param dest_path: 本地目标路径（父目录会自动创建）
        :return: 落盘后的路径
        :raises AdapterError: 调用失败
        """
        if not self.config.is_configured:
            raise AdapterError(
                message=f'审批系统未配置（缺少 {self.config.missing_names}），无法下载附件',
                retryable=False,
            )

        url = f'{self._base()}/docs/{doc_id}/attachment'
        response = self._request('GET', url)

        target = Path(dest_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(response.content)
        logger.info(f'[回写] 已下载审批附件 {doc_id} → {target}（{len(response.content)} 字节）')
        return str(target)


class MockApproveAdapter:
    """
    审批系统 mock Adapter

    未配置 `APPROVE_API_BASE` / `APPROVE_TOKEN` 且允许降级时使用。
    **不假装成功**：它只把评论写进日志并返回一个带 `MOCK-` 前缀的假评论 ID，
    节点会把 `writeback_is_mock=True` 一路透传到状态与评论正文里，
    前端据此明确提示"未真正送达审批系统"。
    """

    name = 'mock'
    is_mock = True

    def __init__(self, reason: str = '审批系统未配置'):
        self.reason = reason
        self._warned = False

    def _warn_once(self) -> None:
        if not self._warned:
            logger.warning(
                f'[回写] 正在使用 mock 审批 Adapter（{self.reason}）—— '
                f'评论只写进日志，**不会真正送达审批系统**，不可用于生产'
            )
            self._warned = True

    def send_comment(self, doc_id: str, content_md: str, idempotency_key: str = '',
                     task_id: str = '', operator: str = 'system') -> Dict[str, Any]:
        """把评论写进日志并返回假评论 ID"""
        self._warn_once()
        mock_comment_id = f'MOCK-CMT-{secrets.token_hex(4)}'
        logger.info(
            f'[回写][MOCK] 单据={doc_id} 任务={task_id} 幂等键={idempotency_key or "(空)"}\n'
            f'---------- 评论正文开始 ----------\n{content_md}\n---------- 评论正文结束 ----------'
        )
        return {'comment_id': mock_comment_id, 'duplicated': False, 'raw': {'_mock': True}}

    def fetch_doc(self, doc_id: str) -> Dict[str, Any]:
        """mock 下返回一个最小单据结构"""
        self._warn_once()
        return {'doc_id': doc_id, '_mock': True}

    def download_attachment(self, doc_id: str, dest_path: str) -> str:
        """mock 下无法下载附件 → 明确报不可重试错误，不产出假文件"""
        raise AdapterError(
            message=f'审批系统未配置，无法下载单据 {doc_id} 的附件（mock Adapter 不产出假文件）',
            retryable=False,
        )


def build_adapter(config: Optional[WritebackConfig] = None,
                  force_mock: bool = False):
    """
    按配置决定使用哪个 Adapter（与 utils/llm_utils._build_provider 同一模式）
    :param config: 配置对象，默认取全局单例
    :param force_mock: 调用方强制要求 mock
    :return: Adapter 实例
    :raises AdapterError: 未配置且不允许 mock 时
    """
    config = config or WritebackConfig.from_env()

    if force_mock:
        return MockApproveAdapter(reason='调用方强制使用 mock')

    if config.is_configured:
        logger.info(
            f'[回写] 使用审批系统 {config.approve_api_base} '
            f'(token={config.masked_token}, timeout={config.approve_timeout}s)'
        )
        return ApproveAdapter(config)

    if config.allow_mock_adapter:
        return MockApproveAdapter(reason=f'.env 未配置 {config.missing_names}')

    raise AdapterError(
        message=f'审批系统未配置（缺少 {config.missing_names}），'
                f'且 WRITEBACK_ALLOW_MOCK_ADAPTER=false，无法执行回写',
        retryable=False,
    )


if __name__ == '__main__':
    # 自测：不联网校验的部分用替身 http 层来驱动（真实联调见 main_graph 的端到端用例）
    import logging as _logging

    _logging.basicConfig(level=_logging.ERROR, format='%(levelname)s - %(message)s')
    passed = 0

    def _config(**overrides) -> WritebackConfig:
        """构造一个配置齐全的 Adapter 配置"""
        base = dict(approve_api_base='http://127.0.0.1:9/mock-approve', approve_token='t')
        base.update(overrides)
        return WritebackConfig(**base)

    # ---------- 一、地址拼接与请求头 ----------
    adapter = ApproveAdapter(_config(approve_api_base='http://x/mock-approve/'))
    assert adapter._base() == 'http://x/mock-approve', '末尾斜杠应被去掉，避免拼出 //'
    assert adapter._headers()['Authorization'] == 'Bearer t'
    assert adapter.is_mock is False and adapter.name == 'http'
    passed += 1
    print('  ✅ 基础地址规范化与 Bearer 令牌头')

    # ---------- 二、错误分类：4xx 不可重试，5xx 可重试 ----------
    def _fake_request_factory(status_code: int, text: str = 'oops'):
        """伪造 httpx.request，返回指定状态码"""
        class _Response:
            def __init__(self):
                self.status_code = status_code
                self.text = text

            @property
            def is_success(self):
                return 200 <= self.status_code < 300

        return lambda *args, **kwargs: _Response()

    import httpx as _httpx

    original_request = _httpx.request
    try:
        cases = [
            (404, False, '单据不存在，重试无意义'),
            (422, False, '请求非法，重试无意义'),
            (401, False, '令牌错误，重试无意义'),
            (500, True, '服务端错误，可重试'),
            (503, True, '服务端不可用，可重试'),
            (429, True, '限流，可重试'),
            (408, True, '请求超时，可重试'),
        ]
        for status_code, expected_retryable, label in cases:
            _httpx.request = _fake_request_factory(status_code, 'body-snippet')
            try:
                adapter._request('POST', 'http://x/y')
            except AdapterError as e:
                assert e.retryable is expected_retryable, \
                    f'{status_code}（{label}）应为 retryable={expected_retryable}，实际 {e.retryable}'
                assert str(status_code) in str(e), '报错应包含状态码'
                assert 'body-snippet' in str(e), '报错应带响应体片段便于排查'
                assert e.status_code == status_code
            else:
                raise AssertionError(f'{status_code} 应抛出 AdapterError')
        passed += 1
        print('  ✅ 4xx 标记为不可重试、5xx/408/429 标记为可重试，报错带状态码与响应体')

        # 网络异常与超时
        def _raise_timeout(*args, **kwargs):
            raise _httpx.TimeoutException('timed out')

        _httpx.request = _raise_timeout
        try:
            adapter._request('POST', 'http://x/y')
        except AdapterError as e:
            assert e.retryable is True, '超时应可重试'
            assert '超时' in str(e)
        else:
            raise AssertionError('超时应抛 AdapterError')

        def _raise_connect(*args, **kwargs):
            raise _httpx.ConnectError('connection refused')

        _httpx.request = _raise_connect
        try:
            adapter._request('POST', 'http://x/y')
        except AdapterError as e:
            assert e.retryable is True, '连接失败应可重试'
            assert '不可达' in str(e)
        else:
            raise AssertionError('连接失败应抛 AdapterError')
        passed += 1
        print('  ✅ 超时与连接失败均标记为可重试，且报错文案清楚')

        # ---------- 三、成功路径与响应解析 ----------
        def _success(*args, **kwargs):
            class _Response:
                status_code = 200
                text = ''

                @property
                def is_success(self):
                    return True

                def json(self):
                    return {'code': 0, 'data': {'comment_id': 'CMT-abc', 'duplicated': True}}

            return _Response()

        _httpx.request = _success
        result = adapter.send_comment('APV-1', '## 意见', 'k1', '1', 'legal')
        assert result['comment_id'] == 'CMT-abc'
        assert result['duplicated'] is True, '应正确识别对端返回的重复标记'

        # 对端返回非 JSON（如网关 HTML 错误页）→ 不抛异常，返回空数据
        def _html_success(*args, **kwargs):
            class _Response:
                status_code = 200
                text = '<html>ok</html>'

                @property
                def is_success(self):
                    return True

                def json(self):
                    raise ValueError('not json')

            return _Response()

        _httpx.request = _html_success
        result = adapter.send_comment('APV-1', 'x', 'k2')
        assert result['comment_id'] == '' and result['raw'] == {}, \
            '非 JSON 响应应安全降级为空数据，而不是崩掉'
        passed += 1
        print('  ✅ 成功路径字段解析正确；非 JSON 响应安全降级')

    finally:
        _httpx.request = original_request

    # ---------- 四、未配置时的行为 ----------
    unconfigured = WritebackConfig(approve_api_base='', approve_token='')
    try:
        ApproveAdapter(unconfigured).send_comment('APV-1', 'x')
    except AdapterError as e:
        assert e.retryable is False, '未配置属于不可重试'
        assert 'APPROVE_API_BASE' in str(e)
    else:
        raise AssertionError('未配置时不应发起请求')
    passed += 1
    print('  ✅ 未配置时直接抛不可重试的 AdapterError，不会真的发请求')

    # ---------- 五、工厂函数 ----------
    mock_adapter = build_adapter(WritebackConfig(approve_api_base='', approve_token=''))
    assert mock_adapter.is_mock is True, '未配置且允许 mock 时应返回 MockApproveAdapter'
    result = mock_adapter.send_comment('APV-1', '## 意见', 'k')
    assert result['comment_id'].startswith('MOCK-CMT-'), 'mock 评论 ID 必须带 MOCK- 前缀'
    assert result['raw']['_mock'] is True

    real_adapter = build_adapter(_config())
    assert real_adapter.is_mock is False and real_adapter.name == 'http'

    try:
        build_adapter(WritebackConfig(approve_api_base='', approve_token='',
                                     allow_mock_adapter=False))
    except AdapterError as e:
        assert 'WRITEBACK_ALLOW_MOCK_ADAPTER' in str(e), '报错应指明开关名'
    else:
        raise AssertionError('关闭 mock 开关后未配置应报错')

    assert build_adapter(_config(), force_mock=True).is_mock is True, 'force_mock 应返回 mock'
    passed += 1
    print('  ✅ 工厂：未配置→mock、已配置→http、关闭开关→报错、force_mock 生效')

    # ---------- 六、mock 不产出假附件 ----------
    try:
        MockApproveAdapter().download_attachment('APV-1', '/tmp/should_not_exist.docx')
    except AdapterError as e:
        assert e.retryable is False
        assert '不产出假文件' in str(e)
    else:
        raise AssertionError('mock 下不应产出假附件')
    passed += 1
    print('  ✅ mock Adapter 不产出假附件，而是明确报错')

    print(f'approve_adapter 自测全部通过 ✅ 共 {passed} 个用例')
