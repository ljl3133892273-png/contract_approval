# 合同审批审查系统（后端）

智能合同审查后端：**解析 → 审查（规则 + LLM 语义）→ 建议推荐 → 审批回写 → 报告导出**。

- 语言/框架：Python 3.11+ / FastAPI / SQLAlchemy 2.x（演示 SQLite，生产 MySQL 8 / PostgreSQL 15）
- 文档解析：PyMuPDF（PDF）+ python-docx（DOCX）+ PaddleOCR（图片，可选）
- LLM：OpenAI 兼容 Provider（国内端点，`.env` 配置）
- 流程编排：4 个独立 LangGraph 子图（parse → review → recommend → writeback），Web 层顺序调用

## 快速开始（本地开发）

```bash
# 1.依赖（uv 管理，国内镜像已在 pyproject.toml 配好）
uv sync

# 2.配置
cp .env.example .env      # 按需填写 LLM_API_KEY / APPROVE_API_BASE 等（见下文）

# 3.启动（建表 + 种子数据 + 冒烟均自动完成且幂等）
python main.py            # 或 python -m uvicorn web.main:app --port 8000

# 4.另开终端起 mock 审批服务（联调用；接真实审批系统后不需要）
python -m uvicorn mock_approve_service.main:app --port 9000
```

接口文档：`http://127.0.0.1:8000/docs`

## 演示账号（db/seed.py，生产必须改密码）

| 用户名 | 密码 | 角色 |
| --- | --- | --- |
| `legal` | `legal123` | 法务审查人 |
| `business` | `business123` | 业务经办人 |
| `admin` | `admin123` | 系统管理员 |

## Docker 演示部署（SPEC 14.1）

```bash
docker compose up -d    # backend(8000) + mock-approve(9000)，SQLite 挂 ./data 卷
```

接真实审批系统：删除 compose 里的 mock-approve 服务，把 `APPROVE_API_BASE` 指向真实端点。

## 测试

```bash
# 全部模块自测（72 个模块 / 400+ 用例，均不依赖外部服务）
export PYTHONPATH="$PWD"
for m in db.seed processor.parse_processor.main_graph ...; do python -m $m; done

# HTTP 端到端（起 mock 审批服务 + 注入 LLM 替身，跑完整业务闭环，18 用例）
python scripts/run_e2e.py
```

**约定：凡涉及 LLM 的自测必须注入替身**（实测单次约 34 秒），E2E 里全程零真实 API 调用。

## 需要你填写的配置（`.env`，禁止提交真实密钥）

| 变量 | 说明 |
| --- | --- |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | LLM Provider（不配置则审查走规则层 + mock 降级） |
| `APPROVE_API_BASE` / `APPROVE_TOKEN` | 审批系统端点（演示指 mock：`http://127.0.0.1:9000/mock-approve`） |
| `APP_SECRET_KEY` | JWT 签名密钥（>=32 随机字符，不配置则用演示密钥并告警） |
| `DATABASE_URL` | 默认 `sqlite:///./data/app.db` |
| `FILE_STORE_DIR` / `REPORT_STORE_DIR` | 合同文件 / 报告存放目录 |

## 目录速览

```
processor/            4 个子图（parse/review/recommend/writeback）
web/                  FastAPI 接入层（api/ 路由、orchestrator 编排、security 鉴权）
db/                   模型 / 会话 / 种子数据
utils/                状态机、diff、PDF、LLM Provider、节点进度等
mock_approve_service/ 本地 mock 审批服务（联调与失败注入用）
scripts/run_e2e.py    HTTP 端到端验证脚本
main.py               启动入口
```
