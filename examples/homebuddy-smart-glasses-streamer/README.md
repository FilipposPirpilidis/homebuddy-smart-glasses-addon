# HomeBuddy Smart Glasses Streamer

macOS console application that:

- captures microphone audio
- converts it to PCM16 mono at 16 kHz or encodes Opus at 16 kHz mono
- connects to Home Assistant `/api/websocket`
- streams live microphone audio continuously while running
- supports `transcription` mode or `agent` mode
- prints `transcript_chunk`, final `transcript`, and `agent_response` events

## Build

```bash
cd examples/homebuddy-smart-glasses-streamer
swift build
```

## Run

Transcription mode:

```bash
cd examples/homebuddy-smart-glasses-streamer
swift run homebuddy-smart-glasses-streamer \
  --host homeassistant.local \
  --port 8123 \
  --ha-token <your_home_assistant_token> \
  --mode transcription
```

Agent mode:

```bash
cd examples/homebuddy-smart-glasses-streamer
swift run homebuddy-smart-glasses-streamer \
  --host homeassistant.local \
  --port 8123 \
  --ha-token <your_home_assistant_token> \
  --mode agent
```

Optional flags:

- `--scheme ws|wss`
- `--codec pcm16|opus`
- `--language en`
- `--mode transcription|agent`
- `--ha-token <token>` or `HA_TOKEN=...`

Example with Opus and agent mode:

```bash
cd examples/homebuddy-smart-glasses-streamer
swift run homebuddy-smart-glasses-streamer \
  --host homeassistant.local \
  --port 8123 \
  --ha-token <your_home_assistant_token> \
  --codec opus \
  --mode agent
```

## Notes

- The first run needs microphone permission.
- The terminal or app may need macOS Accessibility permission so the global ESC key monitor works.
- Home Assistant must have the `homebuddy_smart_glasses_service` custom integration installed and configured.
- The integration configuration selects which Home Assistant conversation agent is used when `--mode agent`.
- `--codec opus` uses the native macOS Opus encoder and sends packetized Opus at 16 kHz mono.
