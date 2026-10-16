'''mock_approve_service/main.py'''

'''
审批系统本地模拟服务（演示阶段替身）

为什么需要它:
    SPEC 4.5 的审批回写要有真实的对端才能验证——尤其是这几件事光靠单元测试里的
    假对象测不出来：
      · 幂等键是否真的防住了重复写回（真实系统靠它去重）
      · 超时/503 时的退避重试是否按 5s / 30s 走，且最终落到 failed
      · Bearer Token 是否正确透传
    所以这里起一个行为完整的替身服务，`APPROVE_API_BASE` 指向它即可联调。

启动:
    cd F:/Contract_approval/contract_approval
    export PYTHONPATH="F:/Contract_approval/contract_approval"
    ./.venv/Scripts/python.exe -m uvicorn mock_approve_service.main:app --host 127.0.0.1 --port 9000

    .env 中对应配置：
        APPROVE_API_BASE=http://127.0.0.1:9000/mock-approve
        APPROVE_TOKEN=mock-token          # 与 MOCK_APPROVE_TOKEN 一致（不设则该服务不校验令牌）

接口清单:
    GET    /health                                  健康检查
    POST   /mock-approve/docs                       创建模拟单据（可指定本地附件路径）
    GET    /mock-approve/docs                       列出单据
    GET    /mock-approve/docs/{doc_id}              单据详情（含 attachment_url）
    GET    /mock-approve/docs/{doc_id}/attachment   下载附件
    POST   /mock-approve/docs/{doc_id}/comments     写回评论（**幂等**）
    GET    /mock-approve/docs/{doc_id}/comments     列出评论（可验证幂等效果）
    GET    /mock-approve/_control/failure           查看当前失败注入配置
    POST   /mock-approve/_control/failure           设置失败注入
    DELETE /mock-approve/_control/failure           清除失败注入

身份认证:
    读环境变量 `MOCK_APPROVE_TOKEN`；**设置了才校验**（未设置则不校验，方便本地裸跑）。
    校验方式是标准 `Authorization: Bearer <token>`。

存储:
    进程内内存（dict + 线程锁）。这是模拟服务，重启即清空，不落库。
'''

import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

# 读取 .env —— 让 MOCK_APPROVE_TOKEN 等项目级配置与其余模块口径一致
# （不 override：命令行显式导出的环境变量优先）
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:  # dotenv 是项目依赖，缺失也不该让模拟服务起不来
    pass

logger = logging.getLogger('mock_approve')

# 环境变量名：设置后才校验令牌
TOKEN_ENV = 'MOCK_APPROVE_TOKEN'


# ============================================================
# 请求体模型
# ============================================================
class DocCreateRequest(BaseModel):
    """创建模拟单据"""

    doc_id: Optional[str] = Field(default=None, description='审批单号；不传则自动生成')
    title: str = Field(default='', description='合同/单据标题')
    applicant: str = Field(default='', description='申请人')
    department: str = Field(default='', description='申请部门')
    biz_type: str = Field(default='', description='合同类型（采购/销售/服务/劳动）')
    amount: Optional[float] = Field(default=None, description='合同金额')
    currency: str = Field(default='CNY', description='币种')
    attachment_path: Optional[str] = Field(
        default=None, description='本地附件绝对路径（模拟审批系统侧的合同文件）'
    )


class CommentWriteRequest(BaseModel):
    """写回评论"""

    content_md: str = Field(description='评论正文（Markdown）')
    # 幂等键：SPEC 4.5 要求"task_id + 审批单号"。同一键重复提交必须返回同一条评论。
    idempotency_key: str = Field(default='', description='幂等键')
    task_id: str = Field(default='', description='本地审查任务 ID')
    operator: str = Field(default='system', description='操作人')


class FailureControlRequest(BaseModel):
    """失败注入配置"""

    mode: str = Field(
        default='none', description='none 关闭 / always 一直失败 / next_n 连续 N 次失败 / delay 延迟'
    )
    remaining: int = Field(default=0, description='next_n 模式下剩余失败次数')
    status_code: int = Field(default=503, description='失败时返回的 HTTP 状态码')
    delay_seconds: float = Field(default=0.0, description='delay 模式下睡眠秒数（用于制造超时）')


# ============================================================
# 进程内存储
# ============================================================
@dataclass
class MockDoc:
    """模拟审批单据"""

    doc_id: str
    title: str = ''
    applicant: str = ''
    department: str = ''
    biz_type: str = ''
    amount: Optional[float] = None
    currency: str = 'CNY'
    attachment_path: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec='seconds'))

    def to_dict(self, base_url: str = '') -> Dict[str, Any]:
        data = {
            'doc_id': self.doc_id,
            'title': self.title,
            'applicant': self.applicant,
            'department': self.department,
            'biz_type': self.biz_type,
            'amount': self.amount,
            'currency': self.currency,
            'has_attachment': bool(self.attachment_path),
            'created_at': self.created_at,
        }
        if self.attachment_path:
            data['attachment_url'] = f'{base_url}/mock-approve/docs/{self.doc_id}/attachment'
        return data


@dataclass
class MockComment:
    """模拟评论（回写产物）"""

    comment_id: str
    doc_id: str
    content_md: str
    idempotency_key: str = ''
    task_id: str = ''
    operator: str = 'system'
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec='seconds'))

    def to_dict(self, duplicated: bool = False) -> Dict[str, Any]:
        return {
            'comment_id': self.comment_id,
            'doc_id': self.doc_id,
            'task_id': self.task_id,
            'operator': self.operator,
            'content_length': len(self.content_md or ''),
            'created_at': self.created_at,
            'duplicated': duplicated,
        }


class MockStore:
    """
    进程内存储（线程安全）

    刻意不落库：这是模拟服务，重启清空更符合"演示替身"的定位，
    也避免和主库的 schema 纠缠。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._docs: Dict[str, MockDoc] = {}
        self._comments: Dict[str, List[MockComment]] = {}
        # 幂等索引：(doc_id, idempotency_key) → comment_id
        self._idempotency: Dict[tuple, str] = {}
        self.failure = FailureControlRequest()

    # ---------- 单据 ----------
    def add_doc(self, doc: MockDoc) -> MockDoc:
        with self._lock:
            self._docs[doc.doc_id] = doc
            self._comments.setdefault(doc.doc_id, [])
            return doc

    def get_doc(self, doc_id: str) -> Optional[MockDoc]:
        with self._lock:
            return self._docs.get(doc_id)

    def list_docs(self) -> List[MockDoc]:
        with self._lock:
            return list(self._docs.values())

    # ---------- 评论 ----------
    def add_comment(self, doc_id: str, payload: CommentWriteRequest) -> tuple:
        """
        新增评论；同一 (doc_id, idempotency_key) 重复提交返回已有评论
        :return: (评论对象, 是否重复)
        """
        with self._lock:
            key = (doc_id, payload.idempotency_key or '')
            if payload.idempotency_key:
                existing_id = self._idempotency.get(key)
                if existing_id:
                    for comment in self._comments.get(doc_id, []):
                        if comment.comment_id == existing_id:
                            return comment, True

            comment = MockComment(
                comment_id=f'CMT-{secrets.token_hex(6)}',
                doc_id=doc_id,
                content_md=payload.content_md,
                idempotency_key=payload.idempotency_key,
                task_id=payload.task_id,
                operator=payload.operator,
            )
            self._comments.setdefault(doc_id, []).append(comment)
            if payload.idempotency_key:
                self._idempotency[key] = comment.comment_id
            return comment, False

    def list_comments(self, doc_id: str) -> List[MockComment]:
        with self._lock:
            return list(self._comments.get(doc_id, []))

    def set_failure(self, control: FailureControlRequest) -> FailureControlRequest:
        with self._lock:
            self.failure = control
            return control

    def get_failure(self) -> FailureControlRequest:
        with self._lock:
            return self.failure

    def consume_failure(self) -> Optional[FailureControlRequest]:
        """
        按当前配置判断本次请求是否应当失败（next_n 模式会消耗一次配额）
        :return: 需要失败时返回配置对象；None 表示放行
        """
        with self._lock:
            control = self.failure
            if control.mode == 'none':
                return None
            if control.mode == 'always':
                return control
            if control.mode == 'next_n':
                if control.remaining > 0:
                    control.remaining -= 1
                    return control
                return None
            # delay 模式由调用方单独处理（它最终是成功的）
            return None


STORE = MockStore()


# ============================================================
# 依赖：令牌校验
# ============================================================
def verify_token(authorization: Optional[str] = Header(default=None)) -> None:
    """
    校验 Bearer 令牌

    仅当环境变量 `MOCK_APPROVE_TOKEN` 非空时校验 —— 这样本地裸跑不用配令牌，
    要验证"令牌透传是否生效"时再设上。
    """
    expected = (os.getenv(TOKEN_ENV) or '').strip()
    if not expected:
        return

    if not authorization or not authorization.lower().startswith('bearer '):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='缺少 Authorization: Bearer <token> 请求头',
        )
    if authorization.split(' ', 1)[1].strip() != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='令牌无效',
        )


router = APIRouter(prefix='/mock-approve', tags=['mock-approve'], dependencies=[Depends(verify_token)])


# ============================================================
# 接口实现
# ============================================================
@router.post('/docs')
def create_doc(payload: DocCreateRequest) -> Dict[str, Any]:
    """创建模拟单据（可携带本地附件路径，用于模拟审批系统侧推送的合同文件）"""
    doc_id = payload.doc_id or f'APV-{datetime.now():%Y%m%d}-{secrets.token_hex(3)}'

    if STORE.get_doc(doc_id) is not None:
        raise HTTPException(status_code=409, detail=f'单据 {doc_id} 已存在')

    attachment_path = payload.attachment_path
    if attachment_path and not Path(attachment_path).is_file():
        raise HTTPException(status_code=400, detail=f'附件不存在：{attachment_path}')

    doc = STORE.add_doc(MockDoc(
        doc_id=doc_id,
        title=payload.title,
        applicant=payload.applicant,
        department=payload.department,
        biz_type=payload.biz_type,
        amount=payload.amount,
        currency=payload.currency,
        attachment_path=attachment_path,
    ))
    logger.info(f'[mock-approve] 创建单据 {doc_id}')
    return {'code': 0, 'data': doc.to_dict()}


@router.get('/docs')
def list_docs() -> Dict[str, Any]:
    """列出全部单据"""
    return {'code': 0, 'data': [d.to_dict() for d in STORE.list_docs()]}


@router.get('/docs/{doc_id}')
def get_doc(doc_id: str) -> Dict[str, Any]:
    """查询单据详情（含附件下载地址）"""
    doc = STORE.get_doc(doc_id)
    if doc is None:
        # 模拟真实审批系统的 404 —— Adapter 侧应据此判定"单据不存在"而非"网络异常"
        raise HTTPException(status_code=404, detail=f'单据 {doc_id} 不存在')
    return {'code': 0, 'data': doc.to_dict()}


@router.get('/docs/{doc_id}/attachment')
def download_attachment(doc_id: str):
    """下载合同附件（对应 SPEC 4.1.1 的 approve_push / approve_pull 附件下载）"""
    doc = STORE.get_doc(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f'单据 {doc_id} 不存在')
    if not doc.attachment_path:
        raise HTTPException(status_code=404, detail=f'单据 {doc_id} 没有附件')

    path = Path(doc.attachment_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f'附件文件已丢失：{doc.attachment_path}')

    suffix = path.suffix.lower().lstrip('.')
    media_type = {
        'pdf': 'application/pdf',
        'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'png': 'image/png',
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
    }.get(suffix, 'application/octet-stream')
    return FileResponse(path=str(path), media_type=media_type, filename=path.name)


@router.post('/docs/{doc_id}/comments')
def write_comment(doc_id: str, payload: CommentWriteRequest) -> Dict[str, Any]:
    """
    写回审查评论（SPEC 4.5）

    幂等：同一 `idempotency_key` 重复提交只落一条，返回 `duplicated=True`。
    """
    doc = STORE.get_doc(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f'单据 {doc_id} 不存在')

    # 失败注入（在幂等判定之前 —— 真实的网络故障也是发生在到达业务逻辑之前）
    control = STORE.consume_failure()
    if control is not None:
        logger.warning(f'[mock-approve] 按注入配置返回失败 {control.status_code}')
        raise HTTPException(status_code=control.status_code, detail='注入的模拟故障')

    pending_delay = STORE.get_failure()
    if pending_delay.mode == 'delay' and pending_delay.delay_seconds > 0:
        logger.warning(f'[mock-approve] 按注入配置延迟 {pending_delay.delay_seconds}s')
        time.sleep(pending_delay.delay_seconds)

    if not (payload.content_md or '').strip():
        raise HTTPException(status_code=422, detail='评论正文不能为空')

    comment, duplicated = STORE.add_comment(doc_id, payload)
    logger.info(
        f'[mock-approve] 单据 {doc_id} 写入评论 {comment.comment_id}'
        f'（幂等键={payload.idempotency_key or "(空)"}，重复={duplicated}）'
    )
    return {'code': 0, 'data': comment.to_dict(duplicated=duplicated)}


@router.get('/docs/{doc_id}/comments')
def list_comments(doc_id: str) -> Dict[str, Any]:
    """列出单据下的全部评论（用来验证幂等是否真的防住了重复写回）"""
    if STORE.get_doc(doc_id) is None:
        raise HTTPException(status_code=404, detail=f'单据 {doc_id} 不存在')
    comments = STORE.list_comments(doc_id)
    return {'code': 0, 'total': len(comments), 'data': [c.to_dict() for c in comments]}


# ============================================================
# 控制面：失败注入
# ============================================================
@router.get('/_control/failure')
def get_failure() -> Dict[str, Any]:
    """查看当前失败注入配置"""
    return {'code': 0, 'data': STORE.get_failure().model_dump()}


@router.post('/_control/failure')
def set_failure(payload: FailureControlRequest) -> Dict[str, Any]:
    """
    设置失败注入

    典型用法：
        always   —— 一直失败，验证"重试耗尽后落 failed"
        next_n   —— 连续 N 次失败后成功，验证"退避重试最终成功"
        delay    —— 延迟超过 APPROVE_TIMEOUT，验证客户端超时处理
    """
    if payload.mode not in ('none', 'always', 'next_n', 'delay'):
        raise HTTPException(
            status_code=422,
            detail=f'不支持的 mode={payload.mode!r}，可选 none/always/next_n/delay',
        )
    control = STORE.set_failure(payload)
    logger.warning(f'[mock-approve] 失败注入已设为 {control.model_dump()}')
    return {'code': 0, 'data': control.model_dump()}


@router.delete('/_control/failure')
def clear_failure() -> Dict[str, Any]:
    """清除失败注入，恢复正常"""
    STORE.set_failure(FailureControlRequest())
    return {'code': 0, 'data': {'mode': 'none'}}


# ============================================================
# 应用装配
# ============================================================
app = FastAPI(
    title='合同审批系统 —— 审批侧模拟服务',
    description='供 processor/writeback_processor 的回写 Adapter 联调使用',
    version='1.0.0',
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=False,
    allow_methods=['*'],
    allow_headers=['*'],
)
app.include_router(router)


@app.get('/health')
def health() -> Dict[str, Any]:
    """健康检查（**不需要令牌**，便于探活）"""
    return {
        'status': 'ok',
        'docs': len(STORE.list_docs()),
        'token_required': bool((os.getenv(TOKEN_ENV) or '').strip()),
        'failure_mode': STORE.get_failure().mode,
    }


if __name__ == '__main__':
    # 自测：用 TestClient 把接口行为全跑一遍（无需真的起服务）
    logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')

    import tempfile

    from fastapi.testclient import TestClient

    client = TestClient(app)
    passed = 0

    # 自测必须与 .env 隔离：.env 里配了 MOCK_APPROVE_TOKEN 会让"未带令牌"的用例全部 401。
    # 令牌校验本身在第 6 组用例里显式开关，不依赖外部环境。
    _saved_token_env = os.environ.pop(TOKEN_ENV, None)

    # ---------- 一、健康检查 ----------
    response = client.get('/health')
    assert response.status_code == 200, '健康检查应 200'
    assert response.json()['status'] == 'ok'
    passed += 1
    print('  ✅ /health 正常（无需令牌）')

    # ---------- 二、创建单据 + 附件下载 ----------
    work_dir = Path(tempfile.mkdtemp(prefix='mock_approve_test_'))
    attachment = work_dir / '合同.docx'
    attachment.write_bytes(b'PK\x03\x04 fake docx payload')
    missing_attachment = work_dir / 'not_exists.docx'

    response = client.post('/mock-approve/docs', json={
        'doc_id': 'APV-T-001', 'title': '软件采购合同', 'applicant': '张三',
        'department': '技术部', 'biz_type': '采购', 'amount': 1280000,
        'attachment_path': str(attachment),
    })
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['doc_id'] == 'APV-T-001' and data['has_attachment'] is True
    assert data['attachment_url'].endswith('/mock-approve/docs/APV-T-001/attachment')
    passed += 1
    print('  ✅ 创建单据成功，附件地址正确')

    # 重复创建应 409
    response = client.post('/mock-approve/docs', json={'doc_id': 'APV-T-001'})
    assert response.status_code == 409, '重复单据号应返回 409'
    # 附件路径不存在应 400
    response = client.post('/mock-approve/docs', json={
        'doc_id': 'APV-T-BAD', 'attachment_path': str(missing_attachment),
    })
    assert response.status_code == 400, '附件不存在应返回 400'
    passed += 1
    print('  ✅ 重复单据号 409、附件缺失 400')

    # 附件下载
    response = client.get('/mock-approve/docs/APV-T-001/attachment')
    assert response.status_code == 200, '附件应可下载'
    assert response.content == b'PK\x03\x04 fake docx payload', '下载内容应与原文件一致'
    assert 'wordprocessingml' in response.headers.get('content-type', ''), '应返回 docx 的 MIME'
    # 不存在的单据
    assert client.get('/mock-approve/docs/NOPE').status_code == 404
    assert client.get('/mock-approve/docs/NOPE/attachment').status_code == 404
    passed += 1
    print('  ✅ 附件下载内容与 MIME 正确，未知单据 404')

    # ---------- 三、评论写回 ----------
    response = client.post('/mock-approve/docs/APV-T-001/comments', json={
        'content_md': '## 合同审查意见\n- 风险等级：**高风险**',
        'idempotency_key': '1:APV-T-001', 'task_id': '1', 'operator': 'legal',
    })
    assert response.status_code == 200, response.text
    first = response.json()['data']
    assert first['comment_id'].startswith('CMT-')
    assert first['duplicated'] is False
    assert first['content_length'] > 0
    passed += 1
    print('  ✅ 评论写回成功，返回 comment_id')

    # 空正文应 422
    response = client.post('/mock-approve/docs/APV-T-001/comments',
                           json={'content_md': '   ', 'idempotency_key': 'x'})
    assert response.status_code == 422, '空评论正文应被拒绝'
    # 未知单据 404
    response = client.post('/mock-approve/docs/NOPE/comments',
                           json={'content_md': '内容', 'idempotency_key': 'x'})
    assert response.status_code == 404
    passed += 1
    print('  ✅ 空正文 422、未知单据 404')

    # ---------- 四、幂等（最关键）----------
    response = client.post('/mock-approve/docs/APV-T-001/comments', json={
        'content_md': '## 合同审查意见（第二次提交，内容略有不同）',
        'idempotency_key': '1:APV-T-001', 'task_id': '1',
    })
    again = response.json()['data']
    assert again['duplicated'] is True, '同一幂等键应返回 duplicated=True'
    assert again['comment_id'] == first['comment_id'], '同一幂等键必须返回同一条评论'

    listed = client.get('/mock-approve/docs/APV-T-001/comments').json()
    assert listed['total'] == 1, f'同一幂等键只应落 1 条评论，实际 {listed["total"]} 条'
    passed += 1
    print('  ✅ 幂等键生效：重复提交返回同一条评论，库里只有 1 条')

    # 不同幂等键应新增
    response = client.post('/mock-approve/docs/APV-T-001/comments', json={
        'content_md': '另一份意见', 'idempotency_key': '2:APV-T-001', 'task_id': '2',
    })
    assert response.json()['data']['duplicated'] is False
    assert client.get('/mock-approve/docs/APV-T-001/comments').json()['total'] == 2
    # 幂等键为空时不参与去重（真实系统里空键也不该被当成同一个键）
    client.post('/mock-approve/docs/APV-T-001/comments', json={'content_md': 'A', 'idempotency_key': ''})
    client.post('/mock-approve/docs/APV-T-001/comments', json={'content_md': 'B', 'idempotency_key': ''})
    assert client.get('/mock-approve/docs/APV-T-001/comments').json()['total'] == 4
    passed += 1
    print('  ✅ 不同幂等键各自新增；空幂等键不参与去重')

    # ---------- 五、失败注入 ----------
    # 5.1 always
    client.post('/mock-approve/_control/failure', json={'mode': 'always', 'status_code': 503})
    for _ in range(3):
        response = client.post('/mock-approve/docs/APV-T-001/comments',
                               json={'content_md': 'x', 'idempotency_key': 'fail-key'})
        assert response.status_code == 503, 'always 模式下应一直失败'
    assert client.get('/mock-approve/docs/APV-T-001/comments').json()['total'] == 4, \
        '失败时不应写入评论'
    passed += 1
    print('  ✅ always 模式：连续 3 次 503，且评论未被写入')

    # 5.2 next_n —— 连续 2 次失败后成功（验证退避重试最终成功）
    client.post('/mock-approve/_control/failure',
                json={'mode': 'next_n', 'remaining': 2, 'status_code': 500})
    codes = [
        client.post('/mock-approve/docs/APV-T-001/comments',
                    json={'content_md': '重试测试', 'idempotency_key': 'retry-key'}).status_code
        for _ in range(3)
    ]
    assert codes == [500, 500, 200], f'应为 失败/失败/成功，实际 {codes}'
    assert client.get('/mock-approve/docs/APV-T-001/comments').json()['total'] == 5
    passed += 1
    print('  ✅ next_n 模式：连续 2 次失败后第 3 次成功（可验证退避重试）')

    # 5.3 delay —— 延迟 0.4s（真实用途是超过 APPROVE_TIMEOUT 制造超时）
    client.post('/mock-approve/_control/failure', json={'mode': 'delay', 'delay_seconds': 0.4})
    started = time.time()
    response = client.post('/mock-approve/docs/APV-T-001/comments',
                           json={'content_md': '延迟测试', 'idempotency_key': 'delay-key'})
    elapsed = time.time() - started
    assert response.status_code == 200, 'delay 模式最终应成功'
    assert elapsed >= 0.4, f'应至少延迟 0.4s，实际 {elapsed:.2f}s'
    passed += 1
    print(f'  ✅ delay 模式：延迟 {elapsed:.2f}s 后成功（可用于制造客户端超时）')

    # 5.4 非法 mode
    response = client.post('/mock-approve/_control/failure', json={'mode': '乱填'})
    assert response.status_code == 422, '非法 mode 应被拒绝'
    # 清除
    client.delete('/mock-approve/_control/failure')
    assert client.get('/mock-approve/_control/failure').json()['data']['mode'] == 'none'
    passed += 1
    print('  ✅ 非法 mode 被拒、DELETE 可清除注入')

    # ---------- 六、令牌校验 ----------
    os.environ[TOKEN_ENV] = 'mock-token'
    try:
        assert client.get('/mock-approve/docs').status_code == 401, '无令牌应 401'
        assert client.get('/mock-approve/docs',
                          headers={'Authorization': 'Bearer wrong'}).status_code == 401, \
            '错误令牌应 401'
        assert client.get('/mock-approve/docs',
                          headers={'Authorization': 'mock-token'}).status_code == 401, \
            '缺少 Bearer 前缀应 401'
        assert client.get('/mock-approve/docs',
                          headers={'Authorization': 'Bearer mock-token'}).status_code == 200
        # 健康检查始终放行
        assert client.get('/health').status_code == 200
        assert client.get('/health').json()['token_required'] is True
    finally:
        os.environ.pop(TOKEN_ENV, None)
    passed += 1
    print('  ✅ 令牌校验：无/错误/缺 Bearer 前缀均 401，正确令牌 200，health 始终放行')

    # 还原 .env 里的令牌配置（隔离用）
    if _saved_token_env is not None:
        os.environ[TOKEN_ENV] = _saved_token_env

    print(f'mock_approve_service.main 自测全部通过 ✅ 共 {passed} 个用例')
