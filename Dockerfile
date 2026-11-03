# Image for the multi-container Compose deployment in compose.yaml.
# Each service supplies its own command; this image does not start OpenCode.
FROM node:22-bookworm-slim AS node
FROM python:3.12.14-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends git libpq5 libgomp1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 atlas && useradd -u 10001 -g 10001 -m atlas \
    && install -d -m 0700 -o 10001 -g 10001 /data /data/home
COPY --from=node /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock
COPY . /app
ENV DATA_DIR=/data HOME=/data/home PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    RUNTIME__DLTHUB_TELEMETRY=false DBT_SEND_ANONYMOUS_USAGE_STATS=false
USER 10001:10001
ENTRYPOINT ["/bin/sh", "/app/deploy/python-cache-entrypoint.sh"]
CMD ["uvicorn", "agent_service.api:app", "--host", "0.0.0.0", "--port", "8000"]
