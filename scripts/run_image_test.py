'''scripts/run_image_test.py'''

'''
图片导入测试：把 F:/Contract_approval 下的测试图片走后端完整链路（HTTP 上传 → 解析 → 审查 → 推荐）

重点观察:
    1. 图片格式白名单 / 魔数嗅探 / OCR 分流是否正确；
    2. OCR 引擎：默认**真实识别**（PaddleOCR）；加 --stub 走 mock/替身（快、零外部调用）；
    3. 真实扫描件的识别质量与下游表现（条款切分 / 元数据抽取 / 规则层风险）；
    4. 任务状态、节点进度、风险与建议落库情况。

运行:
    cd F:/Contract_approval/contract_approval
    export PYTHONPATH="F:/Contract_approval/contract_approval"
    ./.venv/Scripts/python.exe scripts/run_image_test.py            # 全真实（OCR + LLM）
    ./.venv/Scripts/python.exe scripts/run_image_test.py --stub     # mock OCR + LLM 替身
'''

import functools
import os
import sys
import tempfile
from pathlib import Path

_WORK_DIR = Path(tempfile.mkdtemp(prefix='image_test_'))
os.environ['FILE_STORE_DIR'] = str(_WORK_DIR / 'files')
os.environ['REPORT_STORE_DIR'] = str(_WORK_DIR / 'reports')

from db.session import init_engine, reset_engine  # noqa: E402
from web import orchestrator  # noqa: E402

USE_STUB = '--stub' in sys.argv

if USE_STUB:
    from processor.parse_processor.main_graph import ContractParseWorkflow  # noqa: E402
    from processor.recommend_processor.main_graph import ContractRecommendWorkflow  # noqa: E402
    from processor.review_processor.main_graph import ContractReviewWorkflow  # noqa: E402

    class _StubProvider:
        """LLM 替身：审查/改写空 items；P1/P2 拿不到字段（等价未命中）"""

        is_mock = False
        name = 'image-test-stub'

        def __init__(self):
            self.call_count = 0

        def chat(self, messages, temperature=None, json_mode=False) -> str:
            self.call_count += 1
            return '{"items": []}'

    stub = _StubProvider()
    orchestrator.ContractParseWorkflow = functools.partial(
        ContractParseWorkflow, provider=stub)
    orchestrator.ContractReviewWorkflow = functools.partial(
        ContractReviewWorkflow, provider=stub)
    orchestrator.ContractRecommendWorkflow = functools.partial(
        ContractRecommendWorkflow, provider=stub)

from fastapi.testclient import TestClient  # noqa: E402

from web.main import create_app  # noqa: E402

reset_engine()
init_engine(f'sqlite:///{(_WORK_DIR / "img.db").as_posix()}')

# 4 张测试图片（1 张销售合同扫描件 + 3 张流程图）
IMAGE_DIR = Path('F:/Contract_approval')
IMAGES = [
    ('测试图片一.png', IMAGE_DIR / '测试图片一.png'),       # 销售合同扫描件（重点观察对象）
    ('后端代码流程图.png', IMAGE_DIR / '后端代码流程图.png'),
    ('后端代码节点流程图.png', IMAGE_DIR / '后端代码节点流程图.png'),
    ('实施步骤流程图.png', IMAGE_DIR / '实施步骤流程图.png'),
]

print('=' * 30 + ' 图片导入后端链路测试 ' + '=' * 30)
print(f'（测试目录：{_WORK_DIR}）\n')

with TestClient(create_app()) as client:
    # 登录两个角色（business 上传 / legal 看风险）
    def _login(username, password):
        resp = client.post('/api/auth/login',
                           json={'username': username, 'password': password})
        return {'Authorization': f"Bearer {resp.json()['data']['access_token']}"}

    biz_h = _login('business', 'business123')
    legal_headers = _login('legal', 'legal123')

    # ---------- 一、逐张上传（TestClient 同步执行后台流水线） ----------
    task_ids = []
    for name, path in IMAGES:
        assert path.is_file(), f'图片不存在：{path}'
        with path.open('rb') as fh:
            resp = client.post(
                '/api/tasks/upload',
                files={'file': (name, fh, 'image/png')},
                data={'contract_name': path.stem, 'run_immediately': 'true'},
                headers=biz_h,
            )
        assert resp.status_code == 201, f'{name} 上传失败：{resp.text}'
        data = resp.json()['data']
        task_ids.append((name, data['task_id']))
        print(f'📤 {name} → 任务 {data["task_id"]}'
              f'（{data["file_size"]} 字节，指纹 {data["file_hash"][:12]}…，类型 {data["file_type"]}）')

    # ---------- 二、逐任务查看运行情况 ----------
    print('\n' + '=' * 30 + ' 各任务运行情况 ' + '=' * 30)
    for name, task_id in task_ids:
        detail = client.get(f'/api/tasks/{task_id}', headers=biz_h).json()['data']
        progress = client.get(f'/api/tasks/{task_id}/progress',
                              headers=biz_h).json()['data']
        risks = client.get(f'/api/tasks/{task_id}/risk-items',
                           headers=legal_headers).json()['data']
        clauses = client.get(f'/api/tasks/{task_id}/clauses',
                             headers=legal_headers).json()['data']

        print(f'\n── {name}（任务 {task_id}）──')
        print(f'   状态: {detail["review_status"]} | 综合风险: {detail["risk_level"] or "未评估"}'
              f' | 进度: {progress["percent"]}%（{progress["finished"]}/{progress["total"]} 节点）')
        if detail.get('blocked_reason'):
            print(f'   ⚠️ 阻塞原因: {detail["blocked_reason"]}')
        meta = detail.get('meta') or {}
        print(f'   元数据: 编号={meta.get("contract_no") or "∅"} '
              f'甲方={meta.get("party_a") or "∅"} 乙方={meta.get("party_b") or "∅"} '
              f'金额={meta.get("amount") or "∅"}')
        print(f'   条款: {clauses["total"]} 条 | 风险: {risks["total"]} 条'
              f'（综合 {risks["overall_risk_level"] or "无"}）')
        for item in risks['items'][:3]:
            print(f'      · [{item["risk_level"]}] {item["dimension"]}/{item["risk_type"]}'
                  f' —— {item["cause"][:44]}')
        if clauses['items']:
            first = clauses['items'][0]
            print(f"   首条条款: {first['clause_id']} 类型={first['clause_type']}"
                  f" 标题={first['title'][:30] or '（无标题）'}")

    # ---------- 三、断言：全部任务应走完链路（mock OCR 不阻塞） ----------
    for name, task_id in task_ids:
        detail = client.get(f'/api/tasks/{task_id}', headers=biz_h).json()['data']
        assert detail['review_status'] == 'completed', \
            f'{name} 应跑完到 completed，实际 {detail["review_status"]}（{detail.get("blocked_reason")}）'

    if USE_STUB:
        print(f'\n✅ 4 张图片全部走完 解析→审查→推荐 链路（LLM 替身调用 {stub.call_count} 次，'
              f'零真实 API；OCR mock 降级，ocr_is_mock=True）')
    else:
        print('\n✅ 4 张图片全部走完 全真实链路（PaddleOCR 真实识别 + 真实 LLM 审查/改写）')

print('\nimage_test 运行完毕 ✅')
