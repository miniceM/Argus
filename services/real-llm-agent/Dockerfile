# Issue #198 独立 Real LLM Agent 镜像；Python 3.12 与 CI 对齐。
FROM docker.io/library/python:3.12.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 仅复制必需文件：requirements.txt 与 app.py，不携带仓库根目录任何内容。
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY app.py /app/app.py

EXPOSE 8080

CMD ["python", "-m", "uvicorn", "--host", "0.0.0.0", "--port", "8080", "app:app"]
