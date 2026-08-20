FROM python:3.13-alpine AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RELAY_DATA_DIR=/data

WORKDIR /app

RUN addgroup -S relay && adduser -S -G relay -u 10001 relay
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY agent_relay ./agent_relay
RUN mkdir -p /data && chown -R relay:relay /data /app

USER relay
EXPOSE 8081 8082
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -q -O - http://127.0.0.1:8082/api/health >/dev/null 2>&1 || exit 1

CMD ["python", "-m", "agent_relay.app"]
