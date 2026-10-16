'''web/errors.py'''

"""
统一响应包与业务错误码（SPEC 7.4）

响应格式:
    成功：{"code": 0, "message": "ok", "data": {...}}
    失败：{"code": <业务码>, "message": "<可读原因>", "data": null}
    分页：在成功包之上额外带 total / page / page_size（SPEC 7.1 的列表响应示例就是如此）

业务码分段（SPEC 只给了 4 个示例码，其余按同一风格补齐）:
    1xxx 接入与文件           1001 文件为空 / 1002 格式不支持 / 1003 超过大小上限 ...
    2xxx 业务状态与流转       2001 回写失败 / 2002 任务已终止 / 2003 只有阻塞任务可重试 ...
    3xxx 鉴权                 3001 未认证 / 3002 无权限 / 3003 登录失败
    4xxx 请求校验             4001 参数非法 / 4004 资源不存在 / 4009 冲突
    5xxx 系统                 5000 内部错误

为什么业务码与 HTTP 状态码要分开:
    HTTP 码给网关/浏览器看（401 该跳登录、403 该提示无权限、404 该提示不存在），
    业务码给前端做文案与埋点。两者语义不同，强行合一会让前端只能靠 message 猜。
"""

from typing import Any, Dict, List, Optional

from fastapi import status
from fastapi.responses import JSONResponse


class ErrorCode:
    """业务错误码（SPEC 7.4）"""

    OK = 0

    # ---------- 1xxx 接入与文件 ----------
    FILE_EMPTY = 1001
    UNSUPPORTED_FORMAT = 1002
    FILE_TOO_LARGE = 1003
    FILE_SAVE_FAILED = 1004
    APPROVAL_FETCH_FAILED = 1005
    APPROVAL_NOT_CONFIGURED = 1006

    # ---------- 2xxx 业务状态与流转 ----------
    WRITEBACK_FAILED = 2001
    TASK_TERMINATED = 2002
    TASK_NOT_RETRYABLE = 2003
    TASK_NOT_COMPLETED = 2004
    NO_EXTERNAL_DOC_ID = 2005
    INVALID_STATUS_TRANSITION = 2006
    PIPELINE_FAILED = 2007
    REPORT_NOT_READY = 2008

    # ---------- 3xxx 鉴权 ----------
    UNAUTHORIZED = 3001
    FORBIDDEN = 3002
    LOGIN_FAILED = 3003

    # ---------- 4xxx 请求校验 ----------
    VALIDATION_FAILED = 4001
    NOT_FOUND = 4004
    CONFLICT = 4009

    # ---------- 5xxx 系统 ----------
    INTERNAL_ERROR = 5000


# 业务码 → 默认 HTTP 状态码（未列出的按 500 处理）
_CODE_TO_HTTP: Dict[int, int] = {
    ErrorCode.OK: status.HTTP_200_OK,
    ErrorCode.FILE_EMPTY: status.HTTP_400_BAD_REQUEST,
    ErrorCode.UNSUPPORTED_FORMAT: status.HTTP_400_BAD_REQUEST,
    ErrorCode.FILE_TOO_LARGE: status.HTTP_413_CONTENT_TOO_LARGE,
    ErrorCode.FILE_SAVE_FAILED: status.HTTP_500_INTERNAL_SERVER_ERROR,
    ErrorCode.APPROVAL_FETCH_FAILED: status.HTTP_502_BAD_GATEWAY,
    ErrorCode.APPROVAL_NOT_CONFIGURED: status.HTTP_503_SERVICE_UNAVAILABLE,
    ErrorCode.WRITEBACK_FAILED: status.HTTP_502_BAD_GATEWAY,
    ErrorCode.TASK_TERMINATED: status.HTTP_409_CONFLICT,
    ErrorCode.TASK_NOT_RETRYABLE: status.HTTP_409_CONFLICT,
    ErrorCode.TASK_NOT_COMPLETED: status.HTTP_409_CONFLICT,
    ErrorCode.NO_EXTERNAL_DOC_ID: status.HTTP_409_CONFLICT,
    ErrorCode.INVALID_STATUS_TRANSITION: status.HTTP_409_CONFLICT,
    ErrorCode.PIPELINE_FAILED: status.HTTP_500_INTERNAL_SERVER_ERROR,
    ErrorCode.REPORT_NOT_READY: status.HTTP_404_NOT_FOUND,
    ErrorCode.UNAUTHORIZED: status.HTTP_401_UNAUTHORIZED,
    ErrorCode.FORBIDDEN: status.HTTP_403_FORBIDDEN,
    ErrorCode.LOGIN_FAILED: status.HTTP_401_UNAUTHORIZED,
    ErrorCode.VALIDATION_FAILED: status.HTTP_400_BAD_REQUEST,
    ErrorCode.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ErrorCode.CONFLICT: status.HTTP_409_CONFLICT,
    ErrorCode.INTERNAL_ERROR: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


class ApiError(Exception):
    """
    业务异常

    节点/服务层抛它，接入层的异常处理器统一转成响应包。
    带 `http_status` 是为了能覆盖默认映射（例如同样是 409，
    "任务已终止" 与 "只有阻塞任务可重试" 对前端的意义不同）。
    """

    def __init__(self, code: int, message: str, http_status: Optional[int] = None,
                 data: Any = None):
        self.code = code
        self.message = message
        self.http_status = http_status or _CODE_TO_HTTP.get(
            code, status.HTTP_500_INTERNAL_SERVER_ERROR
        )
        self.data = data
        super().__init__(f'[{code}] {message}')

    def to_response(self) -> JSONResponse:
        """转成统一响应包"""
        return JSONResponse(
            status_code=self.http_status,
            content={'code': self.code, 'message': self.message, 'data': self.data},
        )


# ============================================================
# 成功响应构造
# ============================================================
def ok(data: Any = None, message: str = 'ok') -> Dict[str, Any]:
    """
    构造成功响应体
    :param data: 业务数据
    :param message: 提示信息
    :return: {'code':0,'message':..,'data':..}
    """
    return {'code': ErrorCode.OK, 'message': message, 'data': data}


def paged(items: List[Any], total: int, page: int, page_size: int,
          message: str = 'ok') -> Dict[str, Any]:
    """
    构造分页响应体

    SPEC 7.1 的列表响应示例把 total / page / page_size 放在**顶层**，
    而 7.4 又要求统一带 code/message/data —— 这里两者都满足：
    data 放数组，分页元信息放顶层。
    """
    return {
        'code': ErrorCode.OK,
        'message': message,
        'data': items,
        'total': total,
        'page': page,
        'page_size': page_size,
    }


def fail(code: int, message: str) -> Dict[str, Any]:
    """构造失���响应体（用于不能抛异常、只能返回的场景）"""
    return {'code': code, 'message': message, 'data': None}


# ============================================================
# 全局异常处理装配
# ============================================================
def register_exception_handlers(app) -> None:
    """
    给 FastAPI 应用注册统一异常处理

    处理四类:
        1. `ApiError`                 —— 业务异常，直接按自带码返回
        2. `RequestValidationError`   —— 参数校验失败 → code=4001，HTTP 400
           （SPEC 7.4「校验失败统一 HTTP 400」，并把出错字段一并返回便于前端定位）
        3. `StarletteHTTPException`   —— 404/405 等框架级异常也套上统一响应包
        4. `Exception`                —— 兜底 500，**只记日志不回显堆栈**（不泄露内部结构）

    :param app: FastAPI 应用实例
    """
    # 延迟导入：本模块在启动早期就会被引用，避免导入期拉起 fastapi 全家桶
    import logging

    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(ApiError)
    async def _handle_api_error(request, exc: ApiError):  # noqa: ANN001
        return exc.to_response()

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(request, exc: RequestValidationError):  # noqa: ANN001
        details = []
        for item in exc.errors():
            location = '.'.join(str(part) for part in item.get('loc', ()))
            details.append(f"{location}: {item.get('msg', '')}")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                'code': ErrorCode.VALIDATION_FAILED,
                'message': '请求参数校验失败：' + '；'.join(details[:5]),
                'data': {'errors': details},
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_error(request, exc: StarletteHTTPException):  # noqa: ANN001
        # 框架抛的 404/405 也走统一包
        code_map = {
            status.HTTP_401_UNAUTHORIZED: ErrorCode.UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN: ErrorCode.FORBIDDEN,
            status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
        }
        if exc.status_code in code_map:
            code = code_map[exc.status_code]
        elif exc.status_code >= 500:
            code = ErrorCode.INTERNAL_ERROR
        else:
            code = ErrorCode.VALIDATION_FAILED
        return JSONResponse(
            status_code=exc.status_code,
            content={'code': code, 'message': str(exc.detail), 'data': None},
        )

    @app.exception_handler(Exception)
    async def _handle_unknown_error(request, exc: Exception):  # noqa: ANN001
        logging.getLogger(__name__).exception('[接入层] 未捕获异常：%s', exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                'code': ErrorCode.INTERNAL_ERROR,
                'message': '服务内部错误，请联系管理员查看日志',
                'data': None,
            },
        )


if __name__ == '__main__':
    # 轻量自测
    passed = 0

    # ---------- 一、成功包 ----------
    body = ok({'id': 1})
    assert body == {'code': 0, 'message': 'ok', 'data': {'id': 1}}
    assert ok().get('data') is None
    passed += 1
    print('  ✅ 成功响应包格式正确')

    # ---------- 二、分页包（同时满足 SPEC 7.1 与 7.4）----------
    body = paged([{'id': 1}], total=42, page=1, page_size=20)
    assert body['code'] == 0 and body['data'] == [{'id': 1}]
    assert body['total'] == 42 and body['page'] == 1 and body['page_size'] == 20
    passed += 1
    print('  ✅ 分页包含 data/total/page/page_size')

    # ---------- 三、错误码映射 ----------
    import json

    err = ApiError(ErrorCode.FILE_EMPTY, '文件为空')
    assert err.http_status == 400, f'1001 应映射 400，实际 {err.http_status}'
    response = err.to_response()
    assert response.status_code == 400
    payload = json.loads(response.body.decode('utf-8'))
    assert payload == {'code': 1001, 'message': '文件为空', 'data': None}
    passed += 1
    print('  ✅ 业务码 → HTTP 码映射正确，响应包结构正确')

    mapping_cases = [
        (ErrorCode.FILE_EMPTY, 400),
        (ErrorCode.UNSUPPORTED_FORMAT, 400),
        (ErrorCode.FILE_TOO_LARGE, 413),
        (ErrorCode.APPROVAL_FETCH_FAILED, 502),
        (ErrorCode.APPROVAL_NOT_CONFIGURED, 503),
        (ErrorCode.WRITEBACK_FAILED, 502),
        (ErrorCode.TASK_TERMINATED, 409),
        (ErrorCode.TASK_NOT_RETRYABLE, 409),
        (ErrorCode.UNAUTHORIZED, 401),
        (ErrorCode.FORBIDDEN, 403),
        (ErrorCode.LOGIN_FAILED, 401),
        (ErrorCode.VALIDATION_FAILED, 400),
        (ErrorCode.NOT_FOUND, 404),
        (ErrorCode.INTERNAL_ERROR, 500),
    ]
    for code, expected_http in mapping_cases:
        assert ApiError(code, 'x').http_status == expected_http, \
            f'{code} 应映射 {expected_http}'
    passed += 1
    print(f'  ✅ {len(mapping_cases)} 个业务码的 HTTP 映射正确')

    # ---------- 四、自定义 HTTP 码可覆盖默认 ----------
    custom = ApiError(ErrorCode.CONFLICT, 'x', http_status=422)
    assert custom.http_status == 422

    # 未登记的业务码兜底 500，而不是崩掉
    unknown = ApiError(999999, '未知码')
    assert unknown.http_status == 500, '未登记的业务码应兜底 500'
    passed += 1
    print('  ✅ 可覆盖 HTTP 码；未登记业务码兜底 500')

    # ---------- 五、SPEC 里点名的 4 个码必须在 ----------
    for attr, code in (('FILE_EMPTY', 1001), ('UNSUPPORTED_FORMAT', 1002),
                       ('WRITEBACK_FAILED', 2001), ('TASK_TERMINATED', 2002)):
        assert getattr(ErrorCode, attr) == code, f'SPEC 规定 {attr} = {code}'
    passed += 1
    print('  ✅ SPEC 点名的 4 个错误码值一致（1001/1002/2001/2002）')

    # ---------- 六、全局异常处理器（用一个最小应用验证）----------
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import BaseModel

    demo_app = FastAPI()
    register_exception_handlers(demo_app)

    class _Payload(BaseModel):
        count: int

    @demo_app.get('/boom')
    def _boom():
        raise ApiError(ErrorCode.TASK_TERMINATED, '任务已终止')

    @demo_app.get('/crash')
    def _crash():
        raise RuntimeError('内部炸了')

    @demo_app.post('/validate')
    def _validate(payload: _Payload):
        return ok(payload.model_dump())

    # raise_server_exceptions=False：TestClient 默认会把服务端异常重新抛出，
    # 那样就测不到 500 兜底处理器了（我们正是要验证它不泄露内部信息）
    demo_client = TestClient(demo_app, raise_server_exceptions=False)

    response = demo_client.get('/boom')
    assert response.status_code == 409
    assert response.json()['code'] == ErrorCode.TASK_TERMINATED

    response = demo_client.post('/validate', json={'count': '不是数字'})
    assert response.status_code == 400, '校验失败应 400'
    payload = response.json()
    assert payload['code'] == ErrorCode.VALIDATION_FAILED
    assert payload['data']['errors'], '应返回出错字段便于前端定位'

    response = demo_client.get('/crash')
    assert response.status_code == 500
    assert response.json()['code'] == ErrorCode.INTERNAL_ERROR
    assert '内部炸了' not in response.json()['message'], \
        '兜底 500 不应把内部异常信息回显给调用方'

    response = demo_client.get('/not-exist')
    assert response.status_code == 404
    assert response.json()['code'] == ErrorCode.NOT_FOUND, '框架级 404 也应套统一包'
    passed += 1
    print('  ✅ 全局异常处理：业务异常/校验失败/500 兜底/框架 404 都走统一响应包')

    print(f'web.errors 自测全部通过 ✅ 共 {passed} 个用例')
