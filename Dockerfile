# 合同审批审查系统后端镜像（演示部署，SPEC 14.1）
#
# 构建:  docker build -t contract-approval .
# 国内加速（SPEC 14.2）:
#   docker build --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple -t contract-approval .

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
    PYTHONPATH=/app

WORKDIR /app

# 依赖层单独构建，利于缓存
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir \
        fastapi "uvicorn[standard]" sqlalchemy python-dotenv \
        python-docx pymupdf openai greenlet reportlab markdown \
        pyjwt bcrypt python-multipart httpx colorlog pydantic

# 应用代码（数据目录通过卷挂载，不进镜像）
COPY . .

EXPOSE 8000

# 启动入口与本地开发完全一致（main.py 里做建表 + 种子 + 冒烟，均幂等）
CMD ["python", "main.py", "--host", "0.0.0.0", "--port", "8000"]
