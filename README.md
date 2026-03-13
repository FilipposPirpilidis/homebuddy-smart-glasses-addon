# HomeBuddy Smart Glasses Add-on

This repository contains:

- A Home Assistant add-on in `homebuddy_smart_glasses`
- A HACS custom integration in `custom_components/homebuddy_smart_glasses_service`

The add-on accepts `pcm16` or `opus` audio and transcribes it with one of these backends:

- `vosk`
- `openai`
- `whisplaybot`

AssemblyAI is intentionally not included.

## Modes

When the client opens a websocket stream through Home Assistant, it must declare a `mode`:

- `transcription`: stream partial and final transcripts
- `agent`: when a final transcript is produced, route it to the configured Home Assistant Assist agent and send both the transcript and the Assist reply back to the client

The Assist agent is selected in the integration configuration from the agents currently available in Home Assistant.

## Installation

### HACS integration

Add this repository as a custom HACS integration repository and install:

```text
HomeBuddy Smart Glasses Service
```

This installs:

```text
/config/custom_components/homebuddy_smart_glasses_service
```

Then restart Home Assistant and add the integration from `Settings -> Devices & Services -> Add Integration`.

### Add-on repository

Add this repository to the Home Assistant Add-on Store and install:

```text
HomeBuddy Smart Glasses Service
```

Recommended initial integration values:

```text
Add-on host: homeassistant.local
Add-on port: 10310
Assist agent: Home Assistant
```

## Add-on configuration

Main options:

- `server_host`
- `server_port`
- `accepted_audio_codecs`
- `language`
- `stt_backend`

Vosk options:

- `model_variant`
- `model_path`

OpenAI options:

- `openai_api_key`
- `openai_realtime_model`
- `openai_transcription_model`
- `openai_prompt`

WhisplayBot options:

- `whisplaybot_recognize_url`
- `whisplaybot_timeout_seconds`
- `whisplaybot_partial_window_seconds`
- `whisplaybot_partial_inference_seconds`
- `whisplaybot_auto_final_silence_ms`
- `whisplaybot_auto_final_min_seconds`
- `whisplaybot_auto_final_silence_level`

## Client protocol

Clients connect to Home Assistant websocket API:

```text
ws://<home-assistant-host>:8123/api/websocket
```

or:

```text
wss://<home-assistant-host>:8123/api/websocket
```

After the normal Home Assistant authentication handshake, use these commands:

- `homebuddy_smart_glasses_service/open_stream`
- `homebuddy_smart_glasses_service/audio_chunk`
- `homebuddy_smart_glasses_service/close_stream`

### `open_stream`

Transcription mode:

```json
{"id":1,"type":"homebuddy_smart_glasses_service/open_stream","mode":"transcription","language":"en","codec":"pcm16","rate":16000,"width":2,"channels":1}
```

Agent mode:

```json
{"id":1,"type":"homebuddy_smart_glasses_service/open_stream","mode":"agent","language":"en","codec":"opus","rate":16000,"width":2,"channels":1}
```

### `audio_chunk`

```json
{"id":2,"type":"homebuddy_smart_glasses_service/audio_chunk","session_id":"139901234560000:1","rate":16000,"width":2,"channels":1,"audio":"<base64-audio>"}
```

### Events

Partial transcript:

```json
{"id":1,"type":"event","event":{"type":"transcript_chunk","text":"turn on the"}}
```

Final transcript in `transcription` mode:

```json
{"id":1,"type":"event","event":{"type":"transcript","text":"turn on the kitchen lights"}}
```

Final transcript plus Assist reply in `agent` mode:

```json
{"id":1,"type":"event","event":{"type":"agent_response","transcript":"turn on the kitchen lights","response":"Done.","conversation_id":"abcd1234","continue_conversation":false,"agent_id":"home_assistant"}}
```

## Notes

- Partial transcript events are still emitted in agent mode.
- The add-on itself remains an STT service. Assist routing happens in the Home Assistant integration after final STT output is received.
- The integration stores the selected Assist agent and uses that agent for every `agent` mode stream.
