#!/usr/bin/with-contenv bashio
set -euo pipefail

SERVER_HOST="$(bashio::config 'server_host')"
SERVER_PORT="$(bashio::config 'server_port')"
WEBSOCKET_HOST="0.0.0.0"
WEBSOCKET_PORT="8099"
ACCEPTED_AUDIO_CODECS="$(bashio::config 'accepted_audio_codecs')"
LANGUAGE="$(bashio::config 'language')"
STT_BACKEND="$(bashio::config 'stt_backend')"
MODEL_VARIANT="$(bashio::config 'model_variant')"
MODEL_PATH="$(bashio::config 'model_path')"
VOSK_GRAMMAR_SENTENCES="$(bashio::config 'vosk_grammar_sentences')"
OPENAI_API_KEY="$(bashio::config 'openai_api_key')"
OPENAI_REALTIME_MODEL="$(bashio::config 'openai_realtime_model')"
OPENAI_TRANSCRIPTION_MODEL="$(bashio::config 'openai_transcription_model')"
OPENAI_PROMPT="$(bashio::config 'openai_prompt')"
WHISPLAYBOT_RECOGNIZE_URL="$(bashio::config 'whisplaybot_recognize_url')"
WHISPLAYBOT_TIMEOUT_SECONDS="$(bashio::config 'whisplaybot_timeout_seconds')"
WHISPLAYBOT_PARTIAL_WINDOW_SECONDS="$(bashio::config 'whisplaybot_partial_window_seconds')"
WHISPLAYBOT_PARTIAL_INFERENCE_SECONDS="$(bashio::config 'whisplaybot_partial_inference_seconds')"
WHISPLAYBOT_AUTO_FINAL_SILENCE_MS="$(bashio::config 'whisplaybot_auto_final_silence_ms')"
WHISPLAYBOT_AUTO_FINAL_MIN_SECONDS="$(bashio::config 'whisplaybot_auto_final_min_seconds')"
WHISPLAYBOT_AUTO_FINAL_SILENCE_LEVEL="$(bashio::config 'whisplaybot_auto_final_silence_level')"

case "${MODEL_VARIANT}" in
  "daanzu")
    DEFAULT_MODEL_PATH="/models/vosk-model-en-us-daanzu-20200905-lgraph"
    ;;
  "zamia")
    DEFAULT_MODEL_PATH="/models/vosk-model-small-en-us-zamia-0.5"
    ;;
  *)
    bashio::log.warning "Unknown model_variant '${MODEL_VARIANT}', falling back to model_path"
    DEFAULT_MODEL_PATH=""
    ;;
esac

if [ "${MODEL_PATH}" = "/models/vosk-model-en-us-daanzu-20200905-lgraph" ] && [ -n "${DEFAULT_MODEL_PATH}" ]; then
  RESOLVED_MODEL_PATH="${DEFAULT_MODEL_PATH}"
else
  RESOLVED_MODEL_PATH="${MODEL_PATH}"
fi

bashio::log.info "Starting HomeBuddy Smart Glasses Service add-on"
bashio::log.info "Listening on ${SERVER_HOST}:${SERVER_PORT}"
bashio::log.info "WebSocket ingress bridge on ${WEBSOCKET_HOST}:${WEBSOCKET_PORT}/ws"
bashio::log.info "Accepted audio codecs ${ACCEPTED_AUDIO_CODECS}"
bashio::log.info "Using STT backend ${STT_BACKEND}"
bashio::log.info "Session mode is selected by the client over the Home Assistant websocket"

cleanup() {
  true
}

if [ "${STT_BACKEND}" = "openai" ]; then
  bashio::log.info "OpenAI backend enabled"
  bashio::log.info "OpenAI realtime session model ${OPENAI_REALTIME_MODEL}"
  bashio::log.info "OpenAI transcription model ${OPENAI_TRANSCRIPTION_MODEL}"
  exec python3 /app.py \
    --listen-host "${SERVER_HOST}" \
    --listen-port "${SERVER_PORT}" \
    --websocket-host "${WEBSOCKET_HOST}" \
    --websocket-port "${WEBSOCKET_PORT}" \
    --accepted-audio-codecs "${ACCEPTED_AUDIO_CODECS}" \
    --language "${LANGUAGE}" \
    --stt-backend "${STT_BACKEND}" \
    --model-path "${RESOLVED_MODEL_PATH}" \
    --openai-api-key "${OPENAI_API_KEY}" \
    --openai-realtime-model "${OPENAI_REALTIME_MODEL}" \
    --openai-transcription-model "${OPENAI_TRANSCRIPTION_MODEL}" \
    --openai-prompt "${OPENAI_PROMPT}"
elif [ "${STT_BACKEND}" = "whisplaybot" ]; then
  bashio::log.info "WhisplayBot backend enabled"
  bashio::log.info "WhisplayBot recognize URL ${WHISPLAYBOT_RECOGNIZE_URL}"
  exec python3 /app.py \
    --listen-host "${SERVER_HOST}" \
    --listen-port "${SERVER_PORT}" \
    --websocket-host "${WEBSOCKET_HOST}" \
    --websocket-port "${WEBSOCKET_PORT}" \
    --accepted-audio-codecs "${ACCEPTED_AUDIO_CODECS}" \
    --language "${LANGUAGE}" \
    --stt-backend "${STT_BACKEND}" \
    --model-path "${RESOLVED_MODEL_PATH}" \
    --whisplay-recognize-url "${WHISPLAYBOT_RECOGNIZE_URL}" \
    --whisplay-timeout-seconds "${WHISPLAYBOT_TIMEOUT_SECONDS}" \
    --whisplay-partial-window-seconds "${WHISPLAYBOT_PARTIAL_WINDOW_SECONDS}" \
    --whisplay-partial-inference-seconds "${WHISPLAYBOT_PARTIAL_INFERENCE_SECONDS}" \
    --whisplay-auto-final-silence-ms "${WHISPLAYBOT_AUTO_FINAL_SILENCE_MS}" \
    --whisplay-auto-final-min-seconds "${WHISPLAYBOT_AUTO_FINAL_MIN_SECONDS}" \
    --whisplay-auto-final-silence-level "${WHISPLAYBOT_AUTO_FINAL_SILENCE_LEVEL}"
else
  bashio::log.info "Using Vosk backend"
  bashio::log.info "Using Vosk model variant ${MODEL_VARIANT}"
  bashio::log.info "Using Vosk model at ${RESOLVED_MODEL_PATH}"
  exec python3 /app.py \
    --listen-host "${SERVER_HOST}" \
    --listen-port "${SERVER_PORT}" \
    --websocket-host "${WEBSOCKET_HOST}" \
    --websocket-port "${WEBSOCKET_PORT}" \
    --accepted-audio-codecs "${ACCEPTED_AUDIO_CODECS}" \
    --language "${LANGUAGE}" \
    --stt-backend "vosk" \
    --model-path "${RESOLVED_MODEL_PATH}" \
    --vosk-grammar-sentences "${VOSK_GRAMMAR_SENTENCES}"
fi
