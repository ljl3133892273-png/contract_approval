'''main.py'''

'''
项目启动入口（后端主服务）

用法:
    cd F:/Contract_approval/contract_approval
    ./.venv/Scripts/python.exe main.py                # 按 .env 的 APP_HOST/APP_PORT 启动
    ./.venv/Scripts/python.exe main.py --port 8080    # 覆盖端口
    ./.venv/Scripts/python.exe main.py --reload       # 开发模式（代码变更自动重载）

等价命令:
    python -m uvicorn web.main:app --host 127.0.0.1 --port 8000

启动时自动完成（幂等，见 web/main.py 的 _bootstrap）:
    建表 → 写种子数据（R-01~R-08 规则 + 7 条示范条款 + 3 个演示账号）→ 规则引擎冒烟

配套服务（按需另开终端启动）:
    mock 审批服务:  python -m uvicorn mock_approve_service.main:app --port 9000
    接口文档:       http://127.0.0.1:8000/docs
'''

import argparse
import sys
from pathlib import Path

# 保证无论从哪里启动都能 import 项目包（PYTHONPATH 兜底）
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description='合同审批审查系统 API 服务')
    parser.add_argument('--host', default=None, help='监听地址（默认读 .env 的 APP_HOST）')
    parser.add_argument('--port', type=int, default=None, help='监听端口（默认读 .env 的 APP_PORT）')
    parser.add_argument('--reload', action='store_true', help='开发模式：代码变更自动重载')
    parser.add_argument('--workers', type=int, default=1, help='工作进程数（生产可加大）')
    args = parser.parse_args()

    import uvicorn

    from config_bag.app_config import get_app_config

    config = get_app_config()
    host = args.host or config.host
    port = args.port or config.port

    # reload 与 workers 互斥（uvicorn 限制），reload 时强制单进程
    workers = 1 if args.reload else max(1, args.workers)

    uvicorn.run(
        'web.main:app',
        host=host,
        port=port,
        reload=args.reload,
        workers=workers,
        log_level='info',
    )


if __name__ == '__main__':
    main()
