from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components import conversation
from homeassistant.components.websocket_api import ActiveConnection
from homeassistant.components.conversation import HOME_ASSISTANT_AGENT
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_ADDON_HOST,
    CONF_ADDON_PORT,
    CONF_AGENT_ID,
    DATA_COMMANDS_REGISTERED,
    DATA_CONFIG,
    DATA_SESSIONS,
    DEFAULT_ADDON_HOST,
    DEFAULT_ADDON_PORT,
    DOMAIN,
    MODE_AGENT,
    MODE_TRANSCRIPTION,
)

LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_ADDON_HOST, default=DEFAULT_ADDON_HOST): cv.string,
                vol.Optional(CONF_ADDON_PORT, default=DEFAULT_ADDON_PORT): cv.port,
                vol.Optional(CONF_AGENT_ID, default=HOME_ASSISTANT_AGENT): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def event_bytes(event_type: str, data: dict[str, Any] | None = None, payload: bytes = b"") -> bytes:
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


@dataclass(slots=True)
class UpstreamConfig:
    host: str
    port: int


class HomeBuddyBridgeSession:
    def __init__(
        self,
        hass: HomeAssistant,
        connection: ActiveConnection,
        subscription_id: int,
        upstream: UpstreamConfig,
        mode: str,
        agent_id: str,
        language: str,
    ):
        self.hass = hass
        self.connection = connection
        self.subscription_id = subscription_id
        self.upstream = upstream
        self.mode = mode
        self.agent_id = agent_id
        self.language = language
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.read_task: asyncio.Task | None = None
        self.closed = False
        self.backend_mode: str | None = None
        self.backend_mode_ready = asyncio.Event()
        self.conversation_id: str | None = None

    async def connect(self, language: str, codec: str, rate: int, width: int, channels: int) -> None:
        self.reader, self.writer = await asyncio.open_connection(self.upstream.host, self.upstream.port)
        self.language = language
        await self.send("transcribe", {"language": language, "mode": self.mode})
        await self.send("audio-start", {"codec": codec, "rate": rate, "width": width, "channels": channels})
        self.read_task = self.hass.loop.create_task(self.read_loop())

    async def read_loop(self) -> None:
        assert self.reader is not None
        try:
            while not self.closed:
                event, _payload = await read_event(self.reader)
                await self.forward_event(event)
        except EOFError:
            LOGGER.info("HomeBuddy upstream session closed")
        except Exception as err:
            LOGGER.exception("HomeBuddy upstream read loop failed")
            self.send_stream_event({"type": "error", "message": str(err)})
        finally:
            await self.close()

    async def forward_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type") or ""
        data = event.get("data") or {}
        if event_type == "transcript-chunk":
            self.send_stream_event({"type": "transcript_chunk", **data})
        elif event_type == "transcript":
            await self.handle_final_transcript((data.get("text") or "").strip())
        elif event_type == "backend":
            mode = (data.get("mode") or "").strip()
            if mode:
                self.backend_mode = mode
                self.backend_mode_ready.set()
        elif event_type == "info":
            self.send_stream_event({"type": "info", **data})
        elif event_type == "error":
            self.send_stream_event({"type": "error", **data})
        elif event_type == "pong":
            self.send_stream_event({"type": "pong", **data})

    async def send(self, event_type: str, data: dict[str, Any] | None = None, payload: bytes = b"") -> None:
        if self.closed or self.writer is None:
            return
        self.writer.write(event_bytes(event_type, data, payload))
        await self.writer.drain()

    async def send_audio_chunk(self, audio_b64: str, rate: int, width: int, channels: int) -> None:
        payload = base64.b64decode(audio_b64)
        await self.send("audio-chunk", {"rate": rate, "width": width, "channels": channels}, payload)

    def send_stream_event(self, event: dict[str, Any]) -> None:
        self.connection.send_message(websocket_api.event_message(self.subscription_id, event))

    async def handle_final_transcript(self, transcript: str) -> None:
        if not transcript:
            return
        if self.mode != MODE_AGENT:
            self.send_stream_event({"type": "transcript", "text": transcript})
            return

        try:
            result = await conversation.async_converse(
                hass=self.hass,
                text=transcript,
                conversation_id=self.conversation_id,
                context=self.connection.context({"id": self.subscription_id, "type": f"{DOMAIN}/open_stream"}),
                language=self.language,
                agent_id=self.agent_id,
            )
        except Exception as err:
            LOGGER.exception("Assist agent request failed")
            self.send_stream_event({"type": "error", "message": str(err), "transcript": transcript})
            return

        self.conversation_id = result.conversation_id
        self.send_stream_event(
            {
                "type": "agent_response",
                "transcript": transcript,
                "response": _extract_agent_response_text(result),
                "conversation_id": result.conversation_id,
                "continue_conversation": result.continue_conversation,
                "agent_id": self.agent_id,
            }
        )

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.read_task is not None:
            self.read_task.cancel()
            self.read_task = None
        if self.writer is not None:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
            self.writer = None
        self.reader = None

    async def wait_for_backend_mode(self, timeout: float = 1.0) -> str | None:
        if self.backend_mode:
            return self.backend_mode
        try:
            await asyncio.wait_for(self.backend_mode_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return self.backend_mode
        return self.backend_mode


def get_upstream_config(hass: HomeAssistant) -> UpstreamConfig:
    cfg = hass.data[DOMAIN][DATA_CONFIG]
    return UpstreamConfig(host=cfg[CONF_ADDON_HOST], port=cfg[CONF_ADDON_PORT])


def get_agent_id(hass: HomeAssistant) -> str:
    cfg = hass.data[DOMAIN][DATA_CONFIG]
    return cfg.get(CONF_AGENT_ID, HOME_ASSISTANT_AGENT)


def _extract_agent_response_text(result: conversation.ConversationResult) -> str:
    response_dict = result.response.as_dict()
    speech = response_dict.get("speech") or {}
    plain = speech.get("plain") or {}
    text = (plain.get("speech") or "").strip()
    if text:
        return text
    ssml = speech.get("ssml") or {}
    return (ssml.get("speech") or "").strip()


def _merged_entry_config(entry: ConfigEntry) -> dict[str, Any]:
    return {
        CONF_ADDON_HOST: entry.options.get(CONF_ADDON_HOST, entry.data.get(CONF_ADDON_HOST, DEFAULT_ADDON_HOST)),
        CONF_ADDON_PORT: entry.options.get(CONF_ADDON_PORT, entry.data.get(CONF_ADDON_PORT, DEFAULT_ADDON_PORT)),
        CONF_AGENT_ID: entry.options.get(CONF_AGENT_ID, entry.data.get(CONF_AGENT_ID, HOME_ASSISTANT_AGENT)),
    }


def _normalize_stream_mode(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized == MODE_AGENT:
        return MODE_AGENT
    return MODE_TRANSCRIPTION


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    domain_config = config.get(DOMAIN, {})
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(
        DATA_CONFIG,
        {
            CONF_ADDON_HOST: domain_config.get(CONF_ADDON_HOST, DEFAULT_ADDON_HOST),
            CONF_ADDON_PORT: domain_config.get(CONF_ADDON_PORT, DEFAULT_ADDON_PORT),
            CONF_AGENT_ID: domain_config.get(CONF_AGENT_ID, HOME_ASSISTANT_AGENT),
        },
    )
    hass.data[DOMAIN].setdefault(DATA_SESSIONS, {})
    _register_commands(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][DATA_CONFIG] = _merged_entry_config(entry)
    hass.data[DOMAIN].setdefault(DATA_SESSIONS, {})
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _register_commands(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    sessions: dict[str, HomeBuddyBridgeSession] = hass.data.get(DOMAIN, {}).get(DATA_SESSIONS, {})
    for session in list(sessions.values()):
        await session.close()
    sessions.clear()
    if DOMAIN in hass.data:
        hass.data[DOMAIN][DATA_CONFIG] = {
            CONF_ADDON_HOST: DEFAULT_ADDON_HOST,
            CONF_ADDON_PORT: DEFAULT_ADDON_PORT,
            CONF_AGENT_ID: HOME_ASSISTANT_AGENT,
        }
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _register_commands(hass: HomeAssistant) -> None:
    if hass.data[DOMAIN].get(DATA_COMMANDS_REGISTERED):
        return
    websocket_api.async_register_command(hass, websocket_open_stream)
    websocket_api.async_register_command(hass, websocket_audio_chunk)
    websocket_api.async_register_command(hass, websocket_close_stream)
    hass.data[DOMAIN][DATA_COMMANDS_REGISTERED] = True


@websocket_api.websocket_command(
    {
        vol.Required("id"): int,
        vol.Required("type"): f"{DOMAIN}/open_stream",
        vol.Optional("mode", default=MODE_TRANSCRIPTION): vol.In([MODE_TRANSCRIPTION, MODE_AGENT]),
        vol.Optional("language", default="en"): str,
        vol.Optional("codec", default="pcm16"): str,
        vol.Optional("rate", default=16000): int,
        vol.Optional("width", default=2): int,
        vol.Optional("channels", default=1): int,
    }
)
@websocket_api.async_response
async def websocket_open_stream(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    upstream = get_upstream_config(hass)
    mode = _normalize_stream_mode(msg.get("mode", MODE_TRANSCRIPTION))
    session = HomeBuddyBridgeSession(
        hass,
        connection,
        msg["id"],
        upstream,
        mode,
        get_agent_id(hass),
        msg["language"],
    )
    await session.connect(msg["language"], msg.get("codec", "pcm16"), msg["rate"], msg["width"], msg["channels"])
    sessions: dict[str, HomeBuddyBridgeSession] = hass.data[DOMAIN][DATA_SESSIONS]
    session_id = f"{id(connection)}:{msg['id']}"
    sessions[session_id] = session

    async def cleanup() -> None:
        sessions.pop(session_id, None)
        await session.close()

    def unsubscribe() -> None:
        hass.async_create_task(cleanup())

    connection.subscriptions[msg["id"]] = unsubscribe
    backend_mode = await session.wait_for_backend_mode()
    result = {"session_id": session_id, "mode": mode, "agent_id": session.agent_id}
    if backend_mode:
        result["backend_mode"] = backend_mode
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("id"): int,
        vol.Required("type"): f"{DOMAIN}/audio_chunk",
        vol.Required("session_id"): str,
        vol.Required("audio"): str,
        vol.Optional("rate", default=16000): int,
        vol.Optional("width", default=2): int,
        vol.Optional("channels", default=1): int,
    }
)
@websocket_api.async_response
async def websocket_audio_chunk(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    session: HomeBuddyBridgeSession | None = hass.data[DOMAIN][DATA_SESSIONS].get(msg["session_id"])
    if session is None:
        connection.send_error(msg["id"], "not_found", "Unknown session_id")
        return
    await session.send_audio_chunk(msg["audio"], msg["rate"], msg["width"], msg["channels"])
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("id"): int,
        vol.Required("type"): f"{DOMAIN}/close_stream",
        vol.Required("session_id"): str,
    }
)
@websocket_api.async_response
async def websocket_close_stream(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    sessions: dict[str, HomeBuddyBridgeSession] = hass.data[DOMAIN][DATA_SESSIONS]
    session = sessions.pop(msg["session_id"], None)
    if session is None:
        connection.send_error(msg["id"], "not_found", "Unknown session_id")
        return
    await session.send("audio-stop", {})
    await session.close()
    connection.send_result(msg["id"])
