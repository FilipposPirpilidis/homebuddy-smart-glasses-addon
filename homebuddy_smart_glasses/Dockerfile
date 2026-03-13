ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.19
FROM ${BUILD_FROM}

ENV PYTHONUNBUFFERED=1
ENV VOSK_MODEL_URL_015=https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
ENV VOSK_MODEL_URL_ZAMIA=https://alphacephei.com/vosk/models/vosk-model-small-en-us-zamia-0.5.zip

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
    && curl -L "${VOSK_MODEL_URL_015}" -o /tmp/vosk-model-015.zip \
    && unzip -q /tmp/vosk-model-015.zip -d /models \
    && curl -L "${VOSK_MODEL_URL_ZAMIA}" -o /tmp/vosk-model-zamia.zip \
    && unzip -q /tmp/vosk-model-zamia.zip -d /models \
    && rm -f /tmp/vosk-model-015.zip /tmp/vosk-model-zamia.zip \
    && rm -rf /var/lib/apt/lists/*

COPY app.py /app.py
COPY run.sh /run.sh

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
