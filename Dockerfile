FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LISTEN_HOST=0.0.0.0 \
    LISTEN_PORT=8091 \
    BASE_PATH=/usage \
    AUTH_MODE=sub2api \
    REFRESH_INTERVAL_SECONDS=60 \
    PUBLIC_DIR=/app/public \
    DATA_FILE=/app/data/data.json \
    QUERY_FILE=/app/query.sql

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY usage_dashboard_server.py /app/usage_dashboard_server.py
COPY query.sql /app/query.sql
COPY index.html /app/public/index.html

RUN useradd --uid 10001 --create-home --home-dir /home/sub2api-usage --shell /usr/sbin/nologin sub2api-usage \
    && mkdir -p /app/data \
    && chown -R sub2api-usage:sub2api-usage /app

USER sub2api-usage
EXPOSE 8091

CMD ["python", "/app/usage_dashboard_server.py"]
