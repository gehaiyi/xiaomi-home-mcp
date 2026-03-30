#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["fastmcp", "aiohttp"]
# ///
"""
Xiaomi Home MCP Server — Control Xiaomi smart home devices via cloud API.

Setup:
    1. Login first:  uv run --script login.py
    2. Start server: uv run --script mcp_server.py

    Or with Claude Code:
        claude mcp add xiaomi-home -- uv run --script /path/to/mcp_server.py

    Or with environment variables (legacy):
        claude mcp add xiaomi-home \
            -e XIAOMI_PASS_TOKEN="..." -e XIAOMI_USER_ID="..." -e XIAOMI_DEVICE_ID="..." \
            -- uv run --script /path/to/mcp_server.py

Tools:
    - get_devices()                — List all devices and online status
    - get_device_status(name?)     — Query device state (power, brightness, temperature, etc.)
    - control_device(name, action) — Control device (on/off, brightness, color_temp, etc.)
    - play_music(keyword)          — Play music on XiaoAi speaker
    - pause_music()                — Pause playback
    - resume_music()               — Resume playback
    - set_volume(volume)           — Set speaker volume
    - tts(text)                    — Text-to-speech on XiaoAi speaker
    - xiaoai_command(command)      — Send voice command to XiaoAi
    - get_speaker_status()         — Get speaker playback status
"""

import json
import logging
import os
import time
from base64 import b64decode, b64encode
from hashlib import sha1, sha256
from hmac import new as hmac_new
from os import urandom
from typing import Optional

import aiohttp
from fastmcp import FastMCP

mcp = FastMCP(
    "xiaomi-home",
    instructions=(
        "Control Xiaomi smart home devices. "
        "Supports lights, AC, heater, fan, speaker, lock, temperature sensors, and more. "
        "Use get_devices() to discover devices, get_device_status() to check state, "
        "and control_device() to control them."
    ),
)
logger = logging.getLogger("xiaomi-home")

# ── Config ──────────────────────────────────────────────

TOKEN_FILE = os.path.expanduser("~/.xiaomi-mcp/tokens.json")


def _load_config() -> tuple[str, str, str]:
    """Load auth credentials: token file first, then environment variables."""
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE) as f:
                tokens = json.load(f)
            pt = tokens.get("passToken", "")
            uid = tokens.get("userId", "")
            did = tokens.get("deviceId", "")
            if pt and uid:
                logger.info("Loaded credentials from %s", TOKEN_FILE)
                return pt, uid, did
        except (json.JSONDecodeError, KeyError):
            pass
    return (
        os.environ.get("XIAOMI_PASS_TOKEN", ""),
        os.environ.get("XIAOMI_USER_ID", ""),
        os.environ.get("XIAOMI_DEVICE_ID", ""),
    )


PASS_TOKEN, USER_ID, DEVICE_ID = _load_config()

# ── Xiaomi Cloud Auth ──────────────────────────────────


class XiaomiAuth:
    """Minimal Xiaomi cloud auth — zero external dependencies."""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.token: dict = {
            "deviceId": DEVICE_ID,
            "userId": USER_ID,
            "passToken": PASS_TOKEN,
        }
        self._sid_tokens: dict = {}  # sid -> (ssecurity, serviceToken)

    async def _service_login(self, uri: str, data=None):
        headers = {
            "User-Agent": "APP/com.xiaomi.mihome APPV/6.0.103 iosPassportSDK/3.9.0 iOS/14.4 miHSTS"
        }
        cookies = {"sdkVersion": "3.9", "deviceId": self.token["deviceId"]}
        if "passToken" in self.token:
            cookies["userId"] = str(self.token["userId"])
            cookies["passToken"] = self.token["passToken"]
        url = "https://account.xiaomi.com/pass/" + uri
        async with self.session.request(
            "GET" if data is None else "POST",
            url,
            data=data,
            cookies=cookies,
            headers=headers,
        ) as r:
            raw = await r.read()
            return json.loads(raw[11:])  # strip &&&START&&& prefix

    async def _get_service_token(self, location: str, nonce: int, ssecurity: str) -> str:
        from urllib.parse import quote

        nsec = "nonce=" + str(nonce) + "&" + ssecurity
        client_sign = b64encode(sha1(nsec.encode()).digest()).decode()
        url = location + "&clientSign=" + quote(client_sign)
        async with self.session.get(
            url, headers={"User-Agent": "APP/com.xiaomi.mihome"}
        ) as r:
            service_token = r.cookies.get("serviceToken")
            if service_token:
                return service_token.value
            raise Exception("Failed to get serviceToken — passToken may be expired")

    async def login(self, sid: str):
        if sid in self._sid_tokens:
            return self._sid_tokens[sid]
        resp = await self._service_login(f"serviceLogin?sid={sid}&_json=true")
        if resp.get("code") != 0:
            raise Exception(f"Login failed for {sid}: {resp}")
        st = await self._get_service_token(
            resp["location"], resp["nonce"], resp["ssecurity"]
        )
        self._sid_tokens[sid] = (resp["ssecurity"], st)
        return resp["ssecurity"], st

    @staticmethod
    def sign_data(uri: str, data, ssecurity: str):
        if not isinstance(data, str):
            data = json.dumps(data)
        nonce = b64encode(
            urandom(8) + int(time.time() / 60).to_bytes(4, "big")
        ).decode()
        snonce_hash = sha256()
        snonce_hash.update(b64decode(ssecurity))
        snonce_hash.update(b64decode(nonce))
        snonce = b64encode(snonce_hash.digest()).decode()
        msg = "&".join([uri, snonce, nonce, "data=" + data])
        sign = hmac_new(
            key=b64decode(snonce), msg=msg.encode(), digestmod=sha256
        ).digest()
        return {
            "_nonce": nonce,
            "data": data,
            "signature": b64encode(sign).decode(),
        }


# ── IoT Service ────────────────────────────────────────


class XiaomiIOService:
    """Xiaomi IoT cloud API client."""

    def __init__(self, auth: XiaomiAuth, server="https://api.io.mi.com/app"):
        self.auth = auth
        self.server = server

    async def _request(self, uri: str, data):
        ssecurity, service_token = await self.auth.login("xiaomiio")
        signed = XiaomiAuth.sign_data(uri, data, ssecurity)
        headers = {
            "User-Agent": "iOS-14.4-6.0.103-iPhone12,3--D7744744F7AF32F0544445285880DD63E47D9BE9-8816080-84A3F44E137B71AE-iPhone",
            "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
        }
        cookies = {
            "userId": str(self.auth.token["userId"]),
            "serviceToken": service_token,
            "PassportDeviceId": self.auth.token["deviceId"],
        }
        async with self.auth.session.post(
            self.server + uri, data=signed, headers=headers, cookies=cookies
        ) as r:
            resp = await r.json(content_type=None)
            if "result" not in resp:
                raise Exception(f"API error {uri}: {resp}")
            return resp["result"]

    async def device_list(self):
        result = await self._request(
            "/home/device_list", {"getVirtualModel": False, "getHuamiDevices": 0}
        )
        return result["list"]

    async def home_request(self, did: str, method: str, params):
        return await self._request(
            "/home/rpc/" + did,
            {
                "id": 1,
                "method": method,
                "accessKey": "IOS00026747c5acafc2",
                "params": params,
            },
        )

    async def miot_action(self, did: str, siid_aiid: list, params: list):
        result = await self._request(
            "/miotspec/action",
            {
                "params": {
                    "did": did,
                    "siid": siid_aiid[0],
                    "aiid": siid_aiid[1],
                    "in": params,
                }
            },
        )
        return result.get("code", -1)

    async def miot_get_props(self, did: str, props: list):
        params = [{"did": did, "siid": s, "piid": p} for s, p in props]
        result = await self._request("/miotspec/prop/get", {"params": params})
        return [r.get("value") for r in result]

    async def miot_set_prop(self, did: str, props: list):
        params = [{"did": did, "siid": s, "piid": p, "value": v} for s, p, v in props]
        result = await self._request("/miotspec/prop/set", {"params": params})
        return result


# ── Speaker Service ────────────────────────────────────


class XiaomiNAService:
    """Xiaomi speaker (MiNA) cloud API client."""

    def __init__(self, auth: XiaomiAuth):
        self.auth = auth

    async def _request(self, uri: str, data=None):
        ssecurity, service_token = await self.auth.login("micoapi")
        headers = {"User-Agent": "MiHome/6.0.103"}
        cookies = {
            "userId": str(self.auth.token["userId"]),
            "serviceToken": service_token,
        }
        method = "POST" if data else "GET"
        async with self.auth.session.request(
            method,
            "https://api2.mina.mi.com" + uri,
            data=data,
            headers=headers,
            cookies=cookies,
        ) as r:
            return await r.json(content_type=None)

    async def device_list(self):
        resp = await self._request("/admin/v2/device_list")
        return resp.get("data", [])

    async def player_set_volume(self, device_id: str, volume: int):
        resp = await self._request(
            "/remote/ubus",
            {
                "deviceId": device_id,
                "message": json.dumps({"volume": volume}),
                "method": "player_set_volume",
                "path": "mediaplayer",
            },
        )
        return resp and resp.get("code") == 0

    async def get_play_status(self, device_id: str):
        return await self._request(
            "/remote/ubus",
            {
                "deviceId": device_id,
                "message": "{}",
                "method": "player_get_play_status",
                "path": "mediaplayer",
            },
        )


# ── Global State ────────────────────────────────────────

_session: Optional[aiohttp.ClientSession] = None
_auth: Optional[XiaomiAuth] = None
_devices: Optional[dict] = None
_devices_expire_at = 0

# Model prefix -> device type mapping
DEVICE_TYPES = {
    "philips.light": "light",
    "yeelink.light": "light",
    "lumi.acpartner": "ac_partner",
    "xiaomi.wifispeaker": "speaker",
    "xiaomi.heater": "heater",
    "xiaomi.fan": "fan",
    "yunmi.kettle": "kettle",
    "loock.lock": "lock",
    "miaomiaoce.sensor_ht": "sensor",
}

# MIoT spec properties per device type: (siid, piid, label)
# These vary by model — verified against miot-spec.com
MIOT_PROPS = {
    "light": [(2, 1, "Power"), (2, 2, "Brightness")],
    "ac_partner": [(2, 1, "Power")],
    "heater": [(2, 1, "Power"), (2, 2, "Target Temperature")],
    "fan": [(2, 1, "Power"), (2, 2, "Speed"), (2, 4, "Oscillate")],
    "lock": [(3, 1021, "Lock State"), (4, 1003, "Battery")],
    "kettle": [],  # BLE device, cloud read not supported
    "sensor_t9": [
        (3, 1001, "Temperature"),
        (3, 1002, "Humidity"),
        (2, 1003, "Battery"),
    ],
    "sensor_t2": [(2, 1, "Temperature"), (2, 2, "Humidity"), (3, 1, "Battery")],
}

# Human-readable value mappings
VALUE_LABELS = {
    "Power": {True: "ON", False: "OFF"},
    "Lock State": {16: "Locked", 32: "Unlocked", 64: "Ajar"},
    "Oscillate": {True: "ON", False: "OFF"},
}

# Chinese keyword -> device type (for fuzzy matching)
KEYWORD_MAP = {
    "灯": "light",
    "台灯": "light",
    "空调": "ac_partner",
    "音箱": "speaker",
    "小爱": "speaker",
    "风扇": "fan",
    "电暖": "heater",
    "水壶": "kettle",
    "锁": "lock",
    "温度": "sensor",
    "湿度": "sensor",
    "light": "light",
    "lamp": "light",
    "ac": "ac_partner",
    "speaker": "speaker",
    "fan": "fan",
    "heater": "heater",
    "kettle": "kettle",
    "lock": "lock",
    "temp": "sensor",
}


def _classify(model: str) -> str:
    for prefix, dtype in DEVICE_TYPES.items():
        if model.startswith(prefix):
            return dtype
    return "unknown"


def _get_props_key(info: dict) -> str:
    """Get MIOT_PROPS key based on device model."""
    dtype = info["type"]
    if dtype == "sensor":
        model = info["model"]
        if "sensor_ht.t9" in model:
            return "sensor_t9"
        elif "sensor_ht.t2" in model:
            return "sensor_t2"
        return ""
    return dtype


def _format_value(label: str, val):
    if label in VALUE_LABELS and val in VALUE_LABELS[label]:
        return VALUE_LABELS[label][val]
    if label in ("Brightness", "Battery", "Humidity"):
        return f"{val}%"
    if "Temperature" in label:
        return f"{val}°C"
    if label == "Color Temperature":
        return f"{val}K"
    return str(val)


async def _get_auth() -> XiaomiAuth:
    global _session, _auth
    if _auth:
        return _auth
    if not PASS_TOKEN:
        raise Exception(
            "Not authenticated. Run `uv run --script login.py` first, "
            "or set XIAOMI_PASS_TOKEN / XIAOMI_USER_ID / XIAOMI_DEVICE_ID env vars."
        )
    _session = aiohttp.ClientSession()
    _auth = XiaomiAuth(_session)
    return _auth


async def _load_devices(force=False):
    global _devices, _devices_expire_at
    if not force and _devices and _devices_expire_at > time.time():
        return _devices
    auth = await _get_auth()
    svc = XiaomiIOService(auth)
    raw = await svc.device_list()
    _devices = {}
    for d in raw:
        _devices[d["name"]] = {
            "did": str(d["did"]),
            "model": d["model"],
            "token": d["token"],
            "localip": d.get("localip", ""),
            "type": _classify(d["model"]),
            "is_online": d.get("isOnline", False),
        }
    _devices_expire_at = time.time() + 300
    return _devices


def _find_device(query: str) -> tuple[str, dict]:
    if not _devices:
        raise Exception("Device list not loaded")
    if query in _devices:
        return query, _devices[query]
    q = query.lower()
    # Fuzzy match by name, prefer online devices
    matches = [(name, info) for name, info in _devices.items() if q in name.lower()]
    if matches:
        online = [(n, i) for n, i in matches if i["is_online"]]
        return (online or matches)[0]
    # Match by keyword
    for kw, dtype in KEYWORD_MAP.items():
        if kw in query:
            candidates = [
                (n, i) for n, i in _devices.items() if i["type"] == dtype
            ]
            if candidates:
                online = [(n, i) for n, i in candidates if i["is_online"]]
                return (online or candidates)[0]
    raise Exception(f"Device not found: {query}. Available: {', '.join(_devices.keys())}")


def _find_speaker() -> dict:
    for info in (_devices or {}).values():
        if info["type"] == "speaker":
            return info
    raise Exception("No XiaoAi speaker found")


async def _get_speaker_device_id() -> str:
    auth = await _get_auth()
    na = XiaomiNAService(auth)
    devices = await na.device_list()
    if not devices:
        raise Exception("No XiaoAi speaker found")
    return devices[0]["deviceID"]


# ── Tools ───────────────────────────────────────────────


@mcp.tool()
async def get_devices() -> str:
    """List all Xiaomi smart home devices and their online status."""
    devices = await _load_devices(force=True)
    lines = [f"Found {len(devices)} devices:\n"]
    for name, info in devices.items():
        status = "Online" if info["is_online"] else "Offline"
        lines.append(f"  {name} ({info['type']}) — {status}")
        lines.append(f"    Model: {info['model']} | DID: {info['did']}")
    return "\n".join(lines)


@mcp.tool()
async def get_device_status(device_name: str = "") -> str:
    """Query detailed device status (power, brightness, temperature, battery, etc.).

    Args:
        device_name: Device name, supports fuzzy matching (e.g. "lamp", "lock", "AC").
                     Leave empty to query all online devices.
    """
    await _load_devices(force=True)
    auth = await _get_auth()
    svc = XiaomiIOService(auth)

    targets = []
    if device_name:
        name, info = _find_device(device_name)
        targets = [(name, info)]
    else:
        targets = [
            (n, i)
            for n, i in _devices.items()
            if i["is_online"] and _get_props_key(i) in MIOT_PROPS
        ]

    if not targets:
        return "No queryable online devices found"

    results = []
    for name, info in targets:
        props_key = _get_props_key(info)
        props = MIOT_PROPS.get(props_key)
        if not props:
            results.append(f"[{name}] BLE device — cloud status query not supported")
            continue
        try:
            values = await svc.miot_get_props(
                info["did"], [(s, p) for s, p, _ in props]
            )
            lines = [f"[{name}]"]
            for (s, p, label), val in zip(props, values):
                lines.append(f"  {label}: {_format_value(label, val)}")
            results.append("\n".join(lines))
        except Exception as e:
            results.append(f"[{name}] Query failed: {e}")

    return "\n\n".join(results)


@mcp.tool()
async def control_device(device_name: str, action: str, value: str = "") -> str:
    """Control a smart home device.

    Args:
        device_name: Device name, supports fuzzy matching (e.g. "lamp", "AC", "fan")
        action: on/off (all) | brightness 1-100 (light) | color_temp 2700-6500 (light) | target_temp 16-30 (heater)
        value: Parameter value (not needed for on/off)
    """
    await _load_devices()
    name, info = _find_device(device_name)
    did, dtype = info["did"], info["type"]
    auth = await _get_auth()
    svc = XiaomiIOService(auth)

    if action in ("on", "off"):
        if dtype == "ac_partner":
            result = await svc.home_request(did, "toggle_plug", [action])
        else:
            result = await svc.home_request(did, "set_power", [action])
        return f"{name}: {'ON' if action == 'on' else 'OFF'} (result: {result})"

    elif action == "brightness" and dtype == "light":
        result = await svc.home_request(did, "set_bright", [int(value)])
        return f"{name}: brightness set to {value}% (result: {result})"

    elif action == "color_temp" and dtype == "light":
        result = await svc.home_request(did, "set_cct", [int(value)])
        return f"{name}: color temperature set to {value}K (result: {result})"

    elif action == "target_temp" and dtype == "heater":
        result = await svc.miot_set_prop(did, [(2, 2, int(value))])
        return f"{name}: target temperature set to {value}°C (result: {result})"

    return f"Unsupported: {action} for {dtype} device"


@mcp.tool()
async def play_music(keyword: str = "") -> str:
    """Play music on XiaoAi speaker.

    Args:
        keyword: Song name, artist, etc. Empty for recommendations.
    """
    await _load_devices()
    speaker = _find_speaker()
    auth = await _get_auth()
    svc = XiaomiIOService(auth)
    text = f"播放{keyword}" if keyword else "播放音乐"
    result = await svc.miot_action(speaker["did"], [5, 1], [text])
    return f"XiaoAi: {text} (result: {result})"


@mcp.tool()
async def pause_music() -> str:
    """Pause XiaoAi speaker playback."""
    await _load_devices()
    auth = await _get_auth()
    svc = XiaomiIOService(auth)
    result = await svc.miot_action(_find_speaker()["did"], [3, 2], [])
    return f"Paused (result: {result})"


@mcp.tool()
async def resume_music() -> str:
    """Resume XiaoAi speaker playback."""
    await _load_devices()
    auth = await _get_auth()
    svc = XiaomiIOService(auth)
    result = await svc.miot_action(_find_speaker()["did"], [3, 1], [])
    return f"Resumed (result: {result})"


@mcp.tool()
async def set_volume(volume: int) -> str:
    """Set XiaoAi speaker volume.

    Args:
        volume: 0-100
    """
    auth = await _get_auth()
    na = XiaomiNAService(auth)
    device_id = await _get_speaker_device_id()
    result = await na.player_set_volume(device_id, volume)
    return f"Volume set to {volume} (result: {result})"


@mcp.tool()
async def tts(text: str) -> str:
    """Text-to-speech on XiaoAi speaker.

    Args:
        text: Text to speak
    """
    await _load_devices()
    auth = await _get_auth()
    svc = XiaomiIOService(auth)
    speaker = _find_speaker()
    result = await svc.miot_action(speaker["did"], [5, 1], [text])
    return f"XiaoAi speaking: '{text}' (result: {result})"


@mcp.tool()
async def xiaoai_command(command: str) -> str:
    """Send a voice command to XiaoAi (simulates talking to XiaoAi).

    Args:
        command: e.g. "turn on living room light", "set alarm for 8am", "what's the weather"
    """
    await _load_devices()
    auth = await _get_auth()
    svc = XiaomiIOService(auth)
    result = await svc.miot_action(_find_speaker()["did"], [5, 1], [command])
    return f"Sent to XiaoAi: '{command}' (result: {result})"


@mcp.tool()
async def get_speaker_status() -> str:
    """Get XiaoAi speaker playback status."""
    auth = await _get_auth()
    na = XiaomiNAService(auth)
    device_id = await _get_speaker_device_id()
    raw = await na.get_play_status(device_id)
    if not raw or "data" not in raw:
        return "Failed to get status"
    info = json.loads(raw["data"].get("info", "{}"))
    status_map = {0: "Idle", 1: "Loading", 2: "Playing", 3: "Paused"}
    status = status_map.get(info.get("status", -1), "Unknown")
    volume = info.get("volume", "?")
    song = info.get("play_song_detail", {})
    if song:
        return (
            f"Status: {status} | Volume: {volume}\n"
            f"Playing: {song.get('artist', '?')} - {song.get('title', '?')}\n"
            f"Progress: {song.get('position', 0) // 1000}s / {song.get('duration', 0) // 1000}s"
        )
    return f"Status: {status} | Volume: {volume}\nNo content playing"


if __name__ == "__main__":
    mcp.run()
