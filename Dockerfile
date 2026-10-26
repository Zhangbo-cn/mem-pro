# mem-pro：零第三方依赖（numpy 仅作为可选加速，镜像里不装也能跑）
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DB_PATH=/data/mem.db

# 版本可核验：镜像里没有 .git，构建时注入 commit，/version 会原样回显
ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=${GIT_COMMIT}

WORKDIR /app

# numpy 是可选的 dense 检索加速；代码在没有 numpy 时自动回退纯 Python
RUN pip install --no-cache-dir "numpy>=2,<3"

COPY service ./service
COPY tools ./tools

RUN useradd --create-home --uid 10001 app \
 && mkdir -p /data \
 && chown -R app:app /data /app
USER app
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"

CMD ["python", "service/server.py"]
