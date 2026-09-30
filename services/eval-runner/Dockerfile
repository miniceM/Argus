FROM python:3.14-slim

ARG ARGUS_BUILD_ID=dev
ARG RUNNER_VERSION=0.1.0
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ARGUS_BUILD_ID=${ARGUS_BUILD_ID} \
    RUNNER_VERSION=${RUNNER_VERSION}

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
