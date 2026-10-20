# Agent Voice Bridge server image: API + SIP (TLS/SRTP) + speech + the AI assistant.
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends espeak-ng \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# The assistant extras (faster-whisper, Piper, Anthropic/OpenAI SDKs) add ~1 GB.
RUN pip install --no-cache-dir ".[assistant]"
# A natural English voice for Piper; the Whisper model downloads on first start (~1.6 GB).
RUN python -m piper.download_voices en_US-lessac-medium --download-dir /opt/piper
ENV AVB_DB_PATH=/data/bridge.sqlite3 \
    AVB_HOST=127.0.0.1 \
    AVB_CALL_DISPATCHER=bark-sip \
    AVB_MODEL_DIR=/data/models \
    AVB_PIPER_VOICE=/opt/piper/en_US-lessac-medium.onnx \
    PYTHONUNBUFFERED=1
VOLUME /data
CMD ["avb-bridge"]
