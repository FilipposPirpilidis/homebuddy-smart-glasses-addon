ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.19
FROM ${BUILD_FROM}

ENV PYTHONUNBUFFERED=1
ENV VOSK_MODEL_URL_DAANZU=https://alphacephei.com/vosk/models/vosk-model-en-us-daanzu-20200905-lgraph.zip

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libatomic1 \
        libopus0 \
        python3 \
        python3-pip \
        unzip \
    && python3 -m pip install --no-cache-dir --break-system-packages \
        opuslib==3.0.1 \
        vosk==0.3.45 \
        websockets==12.0 \
    && mkdir -p /models \
    && curl -L "${VOSK_MODEL_URL_DAANZU}" -o /tmp/vosk-model-daanzu.zip \
    && unzip -q /tmp/vosk-model-daanzu.zip -d /models \
    && rm -f /tmp/vosk-model-daanzu.zip \
    && rm -rf /var/lib/apt/lists/*

COPY app.py /app.py
COPY run.sh /run.sh

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
