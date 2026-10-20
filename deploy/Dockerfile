# Agent Voice Bridge server image: API + SIP (TLS/SRTP) + espeak-ng speech.
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends espeak-ng \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
ENV AVB_DB_PATH=/data/bridge.sqlite3 \
    AVB_HOST=127.0.0.1 \
    AVB_CALL_DISPATCHER=bark-sip \
    PYTHONUNBUFFERED=1
VOLUME /data
CMD ["avb-bridge"]
