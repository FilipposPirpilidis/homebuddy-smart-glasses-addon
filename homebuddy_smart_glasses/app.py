import argparse
import asyncio
import audioop
import base64
import json
import logging
import re
import struct
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote
from urllib.request import Request, urlopen
import websockets
from websockets.exceptions import ConnectionClosed
from opuslib import Decoder as OpusDecoder
from vosk import KaldiRecognizer, Model, SetLogLevel


LOGGER = logging.getLogger("homebuddy_smart_glasses")
SetLogLevel(-1)

NON_SPEECH_TOKENS = {
    "applause",
    "audio",
    "background",
    "beep",
    "blank",
    "blowing",
    "breath",
    "breathing",
    "buzz",
    "cheering",
    "clapping",
    "clearing",
    "clears",
    "cough",
    "coughing",
    "crowd",
    "exhale",
    "exhaling",
    "gasp",
    "gasping",
    "humming",
    "inhale",
    "inhaling",
    "instrumental",
    "laughter",
    "melody",
    "music",
    "noise",
    "playing",
    "rustling",
    "shuffling",
    "ringing",
    "sigh",
    "sighing",
    "silence",
    "singing",
    "sniff",
    "sniffing",
    "static",
    "throat",
    "wind",
    "whooshing",
}


def event_bytes(event_type: str, data: Optional[dict[str, Any]] = None, payload: bytes = b"") -> bytes:
    header: dict[str, Any] = {"type": event_type}
    if data:
        header["data"] = data
    if payload:
        header["payload_length"] = len(payload)
    return (json.dumps(header, separators=(",", ":")) + "\n").encode("utf-8") + payload


async def read_event(reader: asyncio.StreamReader) -> tuple[dict[str, Any], bytes]:
    line = await reader.readline()
    if not line:
        raise EOFError("Connection closed while reading event header")

    event = json.loads(line.decode("utf-8"))
    data = event.get("data") or {}

    data_length = int(event.get("data_length") or 0)
    if data_length:
        extra = await reader.readexactly(data_length)
        extra_obj = json.loads(extra.decode("utf-8"))
        if isinstance(extra_obj, dict):
            data.update(extra_obj)

    payload_length = int(event.get("payload_length") or 0)
    payload = b""
    if payload_length:
        payload = await reader.readexactly(payload_length)

    event["data"] = data
    return event, payload


def result_text(result_json: str) -> str:
    try:
        result = json.loads(result_json)
    except json.JSONDecodeError:
        return ""

    return (result.get("text") or result.get("partial") or "").strip()


@dataclass(slots=True)
class ServerConfig:
    listen_host: str
    listen_port: int
    websocket_host: str
    websocket_port: int
    accepted_audio_codecs: tuple[str, ...]
    language: str
    model_path: str
    stt_backend: str
    openai_api_key: str
    openai_realtime_model: str
    openai_transcription_model: str
    openai_prompt: str
    whisplaybot_recognize_url: str
    whisplaybot_timeout_seconds: float
    whisplaybot_partial_window_seconds: float
    whisplaybot_partial_inference_seconds: float
    whisplaybot_auto_final_silence_ms: int
    whisplaybot_auto_final_min_seconds: float
    whisplaybot_auto_final_silence_level: int


@dataclass(slots=True)
class AudioState:
    rate: int = 16000
    width: int = 2
    channels: int = 1
    codec: str = "pcm16"
    language: str = "en"
    chunks: bytearray | None = None

    def reset(self) -> None:
        self.chunks = bytearray()


class AudioDecoder:
    async def start(self, state: AudioState) -> None:
        return

    async def decode(self, payload: bytes) -> bytes:
        return payload


class PCM16Decoder(AudioDecoder):
    async def start(self, state: AudioState) -> None:
        if state.width != 2 or state.channels != 1:
            raise RuntimeError("PCM16 mode expects 16-bit mono audio")


class OpusPacketDecoder(AudioDecoder):
    def __init__(self) -> None:
        self.decoder: Optional[OpusDecoder] = None
        self.frame_size = 960

    async def start(self, state: AudioState) -> None:
        if state.channels != 1:
            raise RuntimeError("Opus mode currently supports mono audio only")
        if state.rate != 16000:
            raise RuntimeError("Opus mode currently supports 16000 Hz audio only")
        self.decoder = OpusDecoder(state.rate, state.channels)

    async def decode(self, payload: bytes) -> bytes:
        if not payload:
            return b""
        if self.decoder is None:
            raise RuntimeError("Opus decoder is not initialized")
        try:
            return self.decoder.decode(payload, self.frame_size)
        except Exception as err:
            raise RuntimeError(f"Opus decode failed: {err}") from err


class VoskBackend:
    def __init__(self, cfg: ServerConfig, model: Model, emit_partial, emit_final):
        self.cfg = cfg
        self.model = model
        self.emit_partial = emit_partial
        self.emit_final = emit_final
        self.recognizer: Optional[KaldiRecognizer] = None
        self.last_partial_text = ""

    async def start(self, state: AudioState) -> None:
        if state.width != 2 or state.channels != 1:
            raise RuntimeError("Vosk backend expects PCM16 mono audio")

        self.last_partial_text = ""
        self.recognizer = KaldiRecognizer(self.model, float(state.rate))
        self.recognizer.SetWords(True)

    async def process_chunk(self, payload: bytes, _state: AudioState) -> None:
        recognizer = self.recognizer
        if recognizer is None or not payload:
            return

        if recognizer.AcceptWaveform(payload):
            text = result_text(recognizer.Result())
            if text:
                self.last_partial_text = ""
                await self.emit_final(text)
            return

        text = result_text(recognizer.PartialResult())
        if text and text != self.last_partial_text:
            self.last_partial_text = text
            await self.emit_partial(text)

    async def finish(self) -> None:
        recognizer = self.recognizer
        if recognizer is None:
            return

        text = result_text(recognizer.FinalResult())
        if text:
            await self.emit_final(text)
        self.recognizer = None

    async def close(self) -> None:
        self.recognizer = None


class OpenAIRealtimeBackend:
    def __init__(self, cfg: ServerConfig, emit_partial, emit_final, emit_error):
        self.cfg = cfg
        self.emit_partial = emit_partial
        self.emit_final = emit_final
        self.emit_error = emit_error
        self.websocket = None
        self.receive_task: Optional[asyncio.Task] = None
        self.partial_by_item: dict[str, str] = {}
        self.resample_state = None

    async def start(self, state: AudioState) -> None:
        if state.width != 2 or state.channels != 1:
            raise RuntimeError("OpenAI Realtime backend expects PCM16 mono audio")
        if not self.cfg.openai_api_key:
            raise RuntimeError("OpenAI backend selected but openai_api_key is empty")

        realtime_model = quote(self.cfg.openai_realtime_model, safe="")
        uri = f"wss://api.openai.com/v1/realtime?model={realtime_model}"
        headers = {"Authorization": f"Bearer {self.cfg.openai_api_key}"}
        self.websocket = await websockets.connect(uri, extra_headers=headers, max_size=None)
        self.partial_by_item.clear()
        self.resample_state = None
        self.receive_task = asyncio.create_task(self.receive_loop())

        session_update = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "audio": {
                    "input": {
                        "format": {
                            "type": "audio/pcm",
                            "rate": 24000,
                        },
                        "noise_reduction": {
                            "type": "near_field",
                        },
                        "transcription": {
                            "model": self.cfg.openai_transcription_model,
                            "prompt": self.cfg.openai_prompt,
                            "language": state.language,
                        },
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.5,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 500,
                        },
                    }
                },
                "include": ["item.input_audio_transcription.logprobs"],
            },
        }
        await self.websocket.send(json.dumps(session_update))

    async def process_chunk(self, payload: bytes, state: AudioState) -> None:
        if self.websocket is None or not payload:
            return

        pcm24 = self.resample_to_24k(payload, state)
        if not pcm24:
            return

        event = {
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm24).decode("ascii"),
        }
        await self.websocket.send(json.dumps(event))

    async def finish(self) -> None:
        if self.websocket is None:
            return

        # Inference from the Realtime event naming: force commit of any buffered tail audio on shutdown.
        await self.websocket.send(json.dumps({"type": "input_audio_buffer.commit"}))
        await asyncio.sleep(0.5)

    async def close(self) -> None:
        if self.receive_task is not None:
            self.receive_task.cancel()
            try:
                await self.receive_task
            except asyncio.CancelledError:
                pass
            self.receive_task = None

        if self.websocket is not None:
            await self.websocket.close()
            self.websocket = None

        self.partial_by_item.clear()
        self.resample_state = None

    async def receive_loop(self) -> None:
        websocket = self.websocket
        if websocket is None:
            return

        try:
            async for message in websocket:
                event = json.loads(message)
                event_type = event.get("type", "")
                if event_type == "conversation.item.input_audio_transcription.delta":
                    item_id = event.get("item_id", "")
                    delta = (event.get("delta") or "").strip()
                    if not item_id or not delta:
                        continue
                    updated = f"{self.partial_by_item.get(item_id, '')}{delta}"
                    self.partial_by_item[item_id] = updated
                    await self.emit_partial(updated)
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    item_id = event.get("item_id", "")
                    transcript = (event.get("transcript") or "").strip()
                    if item_id:
                        self.partial_by_item.pop(item_id, None)
                    if transcript:
                        await self.emit_final(transcript)
                elif event_type == "error":
                    message_text = (
                        (event.get("error") or {}).get("message")
                        or event.get("message")
                        or "OpenAI Realtime transcription error"
                    )
                    await self.emit_error(message_text)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            LOGGER.exception("OpenAI Realtime receive loop failed")
            await self.emit_error(str(err))

    def resample_to_24k(self, payload: bytes, state: AudioState) -> bytes:
        if state.rate == 24000:
            return payload

        converted, self.resample_state = audioop.ratecv(
            payload,
            state.width,
            state.channels,
            state.rate,
            24000,
            self.resample_state,
        )
        return converted


class WhisplayBackend:
    def __init__(self, cfg: ServerConfig, emit_partial, emit_final):
        self.cfg = cfg
        self.emit_partial = emit_partial
        self.emit_final = emit_final
        self.raw_pcm = bytearray()
        self.bytes_transcribed_for_partial = 0
        self.last_partial_text = ""
        self.partial_retry_not_before = 0.0
        self.consecutive_silence_bytes = 0
        self.pending_audio_bytes = 0

    async def start(self, state: AudioState) -> None:
        if state.width != 2 or state.channels != 1:
            raise RuntimeError("WhisplayBot backend expects PCM16 mono audio")
        self.raw_pcm.clear()
        self.bytes_transcribed_for_partial = 0
        self.last_partial_text = ""
        self.partial_retry_not_before = 0.0
        self.consecutive_silence_bytes = 0
        self.pending_audio_bytes = 0

    async def process_chunk(self, payload: bytes, state: AudioState) -> None:
        if not payload:
            return
        self.raw_pcm.extend(payload)
        self.pending_audio_bytes += len(payload)
        if is_pcm_chunk_silent(payload, self.cfg.whisplaybot_auto_final_silence_level):
            self.consecutive_silence_bytes += len(payload)
        else:
            self.consecutive_silence_bytes = 0

        partial = await self.maybe_partial(state)
        if partial:
            await self.emit_partial(partial)

        if self.should_auto_finalize(state):
            final_text = await self.finalize(state)
            if final_text:
                await self.emit_final(final_text)
            self.reset_stream_state()

    async def finish(self) -> None:
        return

    async def close(self) -> None:
        self.reset_stream_state()

    async def maybe_partial(self, state: AudioState) -> str:
        now = asyncio.get_running_loop().time()
        if now < self.partial_retry_not_before:
            return ""

        min_bytes_for_partial = max(
            int(float(state.rate) * 2.0 * self.cfg.whisplaybot_partial_window_seconds),
            state.rate,
        )
        pending_bytes = len(self.raw_pcm) - self.bytes_transcribed_for_partial
        if pending_bytes < min_bytes_for_partial:
            return ""

        bytes_per_second = state.rate * 2
        partial_bytes = int(float(bytes_per_second) * self.cfg.whisplaybot_partial_inference_seconds)
        clipped_pcm = bytes(self.raw_pcm[-max(partial_bytes, bytes_per_second):])

        try:
            transcript = await self.transcribe_pcm(clipped_pcm, state.rate)
            filtered = transcript.strip()
            if should_drop_transcript_text(filtered):
                filtered = ""
            previous = self.last_partial_text.strip()
            self.last_partial_text = filtered
            self.bytes_transcribed_for_partial = len(self.raw_pcm)
            self.partial_retry_not_before = 0.0
            if filtered and filtered != previous:
                return filtered
            return ""
        except RuntimeError as err:
            if "busy" in str(err).lower():
                self.partial_retry_not_before = now + 0.75
                return ""
            raise

    async def finalize(self, state: AudioState) -> str:
        if not self.raw_pcm:
            return ""
        try:
            transcript = await self.transcribe_pcm(bytes(self.raw_pcm), state.rate)
            normalized = transcript.strip()
            return normalized or self.last_partial_text.strip()
        except RuntimeError as err:
            if "busy" in str(err).lower():
                return self.last_partial_text.strip()
            raise

    async def transcribe_pcm(self, pcm: bytes, sample_rate: int) -> str:
        wav = encode_wav_pcm16_mono(pcm, sample_rate)
        payload = json.dumps({"base64": base64.b64encode(wav).decode("ascii")}).encode("utf-8")
        request = Request(
            self.cfg.whisplaybot_recognize_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        def _request() -> tuple[int, str]:
            with urlopen(request, timeout=max(self.cfg.whisplaybot_timeout_seconds, 10.0)) as response:
                status = getattr(response, "status", response.getcode())
                body = response.read().decode("utf-8")
                return status, body

        try:
            status, body = await asyncio.to_thread(_request)
        except Exception as err:
            raise RuntimeError(f"WhisplayBot request failed: {err}") from err

        if not (200 <= status <= 299):
            raise RuntimeError(f"WhisplayBot request failed (status {status}): {body}")

        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as err:
            raise RuntimeError(f"WhisplayBot response is malformed: {body}") from err

        error = (decoded.get("error") or "").strip()
        if error:
            raise RuntimeError(error)

        return (decoded.get("recognition") or "").strip()

    def should_auto_finalize(self, state: AudioState) -> bool:
        bytes_per_second = state.rate * 2
        min_bytes = max(int(float(bytes_per_second) * self.cfg.whisplaybot_auto_final_min_seconds), state.rate)
        silence_bytes = int(float(bytes_per_second) * float(self.cfg.whisplaybot_auto_final_silence_ms) / 1000.0)
        return (
            self.pending_audio_bytes >= min_bytes
            and self.consecutive_silence_bytes >= max(silence_bytes, state.rate // 2)
        )

    def reset_stream_state(self) -> None:
        self.raw_pcm.clear()
        self.bytes_transcribed_for_partial = 0
        self.last_partial_text = ""
        self.partial_retry_not_before = 0.0
        self.consecutive_silence_bytes = 0
        self.pending_audio_bytes = 0


def encode_wav_pcm16_mono(pcm: bytes, sample_rate: int) -> bytes:
    channels = 1
    bits_per_sample = 16
    bytes_per_sample = bits_per_sample // 8
    byte_rate = sample_rate * channels * bytes_per_sample
    block_align = channels * bytes_per_sample
    data_size = len(pcm)
    riff_chunk_size = 36 + data_size

    header = b"".join(
        [
            b"RIFF",
            struct.pack("<I", riff_chunk_size),
            b"WAVE",
            b"fmt ",
            struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits_per_sample),
            b"data",
            struct.pack("<I", data_size),
        ]
    )
    return header + pcm


def is_pcm_chunk_silent(chunk: bytes, threshold: int) -> bool:
    if len(chunk) < 2:
        return True

    peak = 0
    for index in range(0, len(chunk) - 1, 2):
        sample = int.from_bytes(chunk[index : index + 2], byteorder="little", signed=True)
        abs_sample = 32767 if sample == -32768 else abs(sample)
        if abs_sample > peak:
            peak = abs_sample
            if peak > threshold:
                return False
    return True


class HomeBuddySession:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        cfg: ServerConfig,
        vosk_model: Optional[Model],
    ):
        self.reader = reader
        self.writer = writer
        self.cfg = cfg
        self.vosk_model = vosk_model
        self.state = AudioState(language=cfg.language)
        self._closed = False
        self.backend = None
        self.decoder: AudioDecoder = PCM16Decoder()
        self._send_lock = asyncio.Lock()

    async def run(self) -> None:
        peer = self.writer.get_extra_info("peername")
        LOGGER.info("Client connected: %s", peer)
        try:
            await self.send_backend_mode()
            while not self._closed:
                event, payload = await read_event(self.reader)
                await self.handle_event(event, payload)
        except EOFError:
            LOGGER.info("Client disconnected: %s", peer)
        except (BrokenPipeError, ConnectionResetError):
            LOGGER.info("Client disconnected during write: %s", peer)
        except Exception:
            LOGGER.exception("Session failure for %s", peer)
        finally:
            await self.close_backend()
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    async def handle_event(self, event: dict[str, Any], payload: bytes) -> None:
        event_type = event.get("type")
        data = event.get("data") or {}

        if event_type == "describe":
            backend_name = {
                "vosk": "Vosk",
                "openai": "OpenAI Realtime",
                "whisplaybot": "WhisplayBot",
            }.get(self.cfg.stt_backend, self.cfg.stt_backend)
            await self.send_event(
                "info",
                {
                    "asr": [
                        {
                            "name": f"HomeBuddy Smart Glasses Service ({backend_name})",
                            "description": "Speech-to-text endpoint for HomeBuddy Smart Glasses Service",
                            "attribution": {"name": "HomeBuddy Smart Glasses Service"},
                            "installed": True,
                            "languages": [self.cfg.language],
                            "version": "1.0.0",
                        }
                    ],
                },
            )
            return

        if event_type == "transcribe":
            language = (data.get("language") or "").strip()
            if language:
                self.state.language = language
            return

        if event_type == "audio-start":
            await self.close_backend()
            self.state.reset()
            self.state.rate = int(data.get("rate") or 16000)
            self.state.width = int(data.get("width") or 2)
            self.state.channels = int(data.get("channels") or 1)
            self.state.codec = normalize_codec_name((data.get("codec") or "pcm16").strip())
            if self.cfg.accepted_audio_codecs and self.state.codec not in self.cfg.accepted_audio_codecs:
                raise RuntimeError(f"Unsupported audio codec '{self.state.codec}'")
            self.decoder = build_audio_decoder(self.state.codec)
            await self.decoder.start(self.state)
            self.backend = self.build_backend()
            await self.backend.start(self.state)
            return

        if event_type == "audio-chunk":
            if self.backend is not None:
                decoded_payload = await self.decoder.decode(payload)
                if decoded_payload:
                    await self.backend.process_chunk(decoded_payload, self.state)
            return

        if event_type == "audio-stop":
            if self.backend is not None:
                await self.backend.finish()
                await self.close_backend()
            self.state.reset()
            return

        if event_type == "ping":
            await self.send_event("pong", data)
            return

    def build_backend(self):
        if self.cfg.stt_backend == "openai":
            return OpenAIRealtimeBackend(self.cfg, self.emit_partial_text, self.emit_final_text, self.emit_error_text)
        if self.cfg.stt_backend == "whisplaybot":
            return WhisplayBackend(self.cfg, self.emit_partial_text, self.emit_final_text)
        if self.vosk_model is None:
            raise RuntimeError("Vosk backend selected but no model is loaded")
        return VoskBackend(self.cfg, self.vosk_model, self.emit_partial_text, self.emit_final_text)

    async def close_backend(self) -> None:
        if self.backend is not None:
            await self.backend.close()
            self.backend = None

    async def send_event(self, event_type: str, data: Optional[dict[str, Any]] = None, payload: bytes = b"") -> None:
        async with self._send_lock:
            self.writer.write(event_bytes(event_type, data, payload))
            try:
                await self.writer.drain()
            except (BrokenPipeError, ConnectionResetError):
                self._closed = True
                raise

    async def emit_partial_text(self, text: str) -> None:
        text = text.strip()
        if text:
            await self.send_event("transcript-chunk", {"text": text})

    async def emit_final_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if should_drop_transcript_text(text):
            return
        await self.send_event("transcript", {"text": text})

    async def emit_error_text(self, message: str) -> None:
        await self.send_event("error", {"message": message})

    async def send_backend_mode(self) -> None:
        await self.send_event("backend", {"mode": self.cfg.stt_backend})

class WebSocketSession(HomeBuddySession):
    def __init__(
        self,
        websocket,
        cfg: ServerConfig,
        vosk_model: Optional[Model],
    ):
        self.websocket = websocket
        super().__init__(None, None, cfg, vosk_model)

    async def run(self) -> None:
        peer = getattr(self.websocket, "remote_address", None)
        LOGGER.info("WebSocket client connected: %s", peer)
        try:
            await self.send_backend_mode()
            async for message in self.websocket:
                event, payload = decode_websocket_event(message)
                await self.handle_event(event, payload)
        except ConnectionClosed:
            LOGGER.info("WebSocket client disconnected: %s", peer)
        except Exception:
            LOGGER.exception("WebSocket session failure for %s", peer)
        finally:
            await self.close_backend()
            try:
                await self.websocket.close()
            except Exception:
                pass

    async def send_event(self, event_type: str, data: Optional[dict[str, Any]] = None, payload: bytes = b"") -> None:
        event: dict[str, Any] = {"type": event_type}
        if data:
            event["data"] = data
        if payload:
            event["payload"] = base64.b64encode(payload).decode("ascii")
        async with self._send_lock:
            try:
                await self.websocket.send(json.dumps(event, separators=(",", ":")))
            except ConnectionClosed:
                self._closed = True
                raise


def decode_websocket_event(message: Any) -> tuple[dict[str, Any], bytes]:
    if isinstance(message, bytes):
        raise ValueError("Binary websocket messages are not supported; send JSON text frames")

    event = json.loads(message)
    if not isinstance(event, dict):
        raise ValueError("WebSocket event must be a JSON object")

    data = event.get("data") or {}
    if not isinstance(data, dict):
        data = {}

    payload = b""
    audio_b64 = (event.get("audio") or "").strip()
    payload_b64 = (event.get("payload") or "").strip()
    encoded = audio_b64 or payload_b64
    if encoded:
        payload = base64.b64decode(encoded)

    return {"type": event.get("type"), "data": data}, payload


def normalize_codec_name(value: str) -> str:
    normalized = (value or "").strip().lower()
    if not normalized:
        return "pcm16"
    aliases = {
        "pcm": "pcm16",
        "s16le": "pcm16",
        "audio/pcm": "pcm16",
        "opus": "opus",
        "audio/opus": "opus",
    }
    return aliases.get(normalized, normalized)


def parse_audio_codecs(raw_value: str) -> tuple[str, ...]:
    raw_value = (raw_value or "").strip()
    if not raw_value:
        return ("pcm16",)

    values: list[str]
    if raw_value.startswith("["):
        try:
            decoded = json.loads(raw_value)
        except json.JSONDecodeError:
            decoded = []
        values = [str(item).strip() for item in decoded if str(item).strip()]
    else:
        normalized = raw_value.replace("\r", "\n").replace(",", "\n")
        values = [item.strip() for item in normalized.splitlines() if item.strip()]

    codecs: list[str] = []
    for value in values:
        normalized = normalize_codec_name(value)
        if normalized:
            codecs.append(normalized)
    return tuple(dict.fromkeys(codecs or ["pcm16"]))


def build_audio_decoder(codec: str) -> AudioDecoder:
    if codec == "opus":
        return OpusPacketDecoder()
    return PCM16Decoder()


def should_drop_transcript_text(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return True

    ignored_values = {
        "[blank_audio]",
        "blank_audio",
        "(blank_audio)",
        "[noise]",
        "(noise)",
        "[silence]",
        "(silence)",
        "[music]",
        "(music)",
        "[applause]",
        "(applause)",
        "[laughter]",
        "(laughter)",
        "[static]",
        "(static)",
        "static",
    }
    if normalized in ignored_values:
        return True

    stripped = normalized.strip("[](){} \t")
    tokens = re.findall(r"[a-z']+", stripped)
    if not tokens:
        return True

    if len(tokens) <= 6 and all(token in NON_SPEECH_TOKENS for token in tokens):
        return True

    wrapped = (
        (normalized.startswith("[") and normalized.endswith("]"))
        or (normalized.startswith("(") and normalized.endswith(")"))
    )
    if wrapped and len(tokens) <= 4 and sum(token in NON_SPEECH_TOKENS for token in tokens) >= max(1, len(tokens) - 1):
        return True
    if wrapped and len(tokens) <= 8 and all(token in NON_SPEECH_TOKENS for token in tokens):
        return True

    if not looks_like_sentence(normalized, tokens):
        return True

    return False


def looks_like_sentence(normalized: str, tokens: list[str]) -> bool:
    if len(tokens) < 2:
        return False

    meaningful_tokens = [token for token in tokens if len(token) > 1]
    if len(meaningful_tokens) < 2:
        return False

    alpha_chars = sum(1 for char in normalized if char.isalpha())
    if alpha_chars < 6:
        return False

    wrapped = (
        (normalized.startswith("[") and normalized.endswith("]"))
        or (normalized.startswith("(") and normalized.endswith(")"))
    )
    if wrapped:
        return False

    return True


async def serve(cfg: ServerConfig) -> None:
    vosk_model = None
    if cfg.stt_backend == "vosk":
        LOGGER.info("Loading Vosk model from %s", cfg.model_path)
        vosk_model = Model(cfg.model_path)
        LOGGER.info("Vosk model loaded")
    if cfg.stt_backend == "openai":
        LOGGER.info(
            "OpenAI Realtime backend enabled with session model %s and transcription model %s",
            cfg.openai_realtime_model,
            cfg.openai_transcription_model,
        )
    if cfg.stt_backend == "whisplaybot":
        LOGGER.info("WhisplayBot backend enabled with recognize URL %s", cfg.whisplaybot_recognize_url)

    async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = HomeBuddySession(reader, writer, cfg, vosk_model)
        await session.run()

    async def on_websocket_connect(websocket) -> None:
        if getattr(websocket, "path", "") not in {"", "/", "/ws"}:
            await websocket.close(code=1008, reason="Unsupported path")
            return
        session = WebSocketSession(websocket, cfg, vosk_model)
        await session.run()

    server = await asyncio.start_server(on_connect, cfg.listen_host, cfg.listen_port)
    websocket_server = await websockets.serve(
        on_websocket_connect,
        cfg.websocket_host,
        cfg.websocket_port,
        max_size=2**24,
        ping_interval=20,
        ping_timeout=20,
    )
    addresses = ", ".join(str(sock.getsockname()) for sock in (server.sockets or []))
    LOGGER.info("HomeBuddy Smart Glasses Service server listening on %s", addresses)
    ws_addresses = ", ".join(str(sock.getsockname()) for sock in (websocket_server.sockets or []))
    LOGGER.info("HomeBuddy Smart Glasses Service WebSocket bridge listening on %s", ws_addresses)
    async with server, websocket_server:
        await asyncio.gather(server.serve_forever(), asyncio.Future())


def parse_args() -> ServerConfig:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, default=10310)
    parser.add_argument("--websocket-host", default="0.0.0.0")
    parser.add_argument("--websocket-port", type=int, default=8099)
    parser.add_argument("--accepted-audio-codecs", default='["pcm16","opus"]')
    parser.add_argument("--language", default="en")
    parser.add_argument("--model-path", default="/models/vosk-model-small-en-us-0.15")
    parser.add_argument("--stt-backend", default="vosk")
    parser.add_argument("--openai-api-key", default="")
    parser.add_argument("--openai-realtime-model", default="gpt-realtime-mini")
    parser.add_argument("--openai-transcription-model", default="gpt-4o-mini-transcribe")
    parser.add_argument("--openai-prompt", default="")
    parser.add_argument("--whisplay-recognize-url", default="http://192.168.2.29:8801/recognize")
    parser.add_argument("--whisplay-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--whisplay-partial-window-seconds", type=float, default=2.0)
    parser.add_argument("--whisplay-partial-inference-seconds", type=float, default=4.0)
    parser.add_argument("--whisplay-auto-final-silence-ms", type=int, default=900)
    parser.add_argument("--whisplay-auto-final-min-seconds", type=float, default=0.8)
    parser.add_argument("--whisplay-auto-final-silence-level", type=int, default=700)
    args = parser.parse_args()
    accepted_audio_codecs = parse_audio_codecs(args.accepted_audio_codecs)

    return ServerConfig(
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        websocket_host=args.websocket_host,
        websocket_port=args.websocket_port,
        accepted_audio_codecs=accepted_audio_codecs,
        language=args.language,
        model_path=args.model_path,
        stt_backend=args.stt_backend,
        openai_api_key=args.openai_api_key,
        openai_realtime_model=args.openai_realtime_model,
        openai_transcription_model=args.openai_transcription_model,
        openai_prompt=args.openai_prompt,
        whisplaybot_recognize_url=args.whisplay_recognize_url,
        whisplaybot_timeout_seconds=args.whisplay_timeout_seconds,
        whisplaybot_partial_window_seconds=args.whisplay_partial_window_seconds,
        whisplaybot_partial_inference_seconds=args.whisplay_partial_inference_seconds,
        whisplaybot_auto_final_silence_ms=args.whisplay_auto_final_silence_ms,
        whisplaybot_auto_final_min_seconds=args.whisplay_auto_final_min_seconds,
        whisplaybot_auto_final_silence_level=args.whisplay_auto_final_silence_level,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = parse_args()
    asyncio.run(serve(cfg))


if __name__ == "__main__":
    main()
