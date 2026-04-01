#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["aiohttp"]
# ///
"""
Xiaomi Home CLI — Control Xiaomi smart home devices from the command line.

Core API shared with mcp_server.py — keep in sync.

Usage:
    uv run --script cli.py devices                       # List all devices
    uv run --script cli.py status                        # All online devices
    uv run --script cli.py status "lamp"                 # Specific device
    uv run --script cli.py control "lamp" on             # Turn on
    uv run --script cli.py control "lamp" off            # Turn off
    uv run --script cli.py control "lamp" brightness 50  # Set brightness
    uv run --script cli.py control "lamp" color_temp 4000
    uv run --script cli.py control "heater" target_temp 22
    uv run --script cli.py play "Jay Chou"               # Play music
    uv run --script cli.py pause                         # Pause
    uv run --script cli.py resume                        # Resume
    uv run --script cli.py volume 30                     # Set volume
    uv run --script cli.py tts "Dinner is ready"         # Text-to-speech
    uv run --script cli.py xiaoai "What's the weather"   # Voice command
    uv run --script cli.py speaker                       # Playback status

Setup:
    1. Login first:  uv run --script login.py
    2. Run commands: uv run --script cli.py <command> [args...]
"""

import asyncio
import json
import os
import sys
import time
from base64 import b64decode, b64encode
from hashlib import sha1, sha256
from hmac import new as hmac_new
from os import urandom
from typing import Optional

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

import aiohttp


class XiaomiAuth:
    """Minimal Xiaomi cloud auth — zero external dependencies."""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.token: dict = {
            "deviceId": DEVICE_ID,
            "userId": USER_ID,
            "passToken": PASS_TOKEN,
        }
        self._sid_tokens: dict = {}

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
            return json.loads(raw[11:])

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


# ── Device Mappings ────────────────────────────────────

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

MIOT_PROPS = {
    "light": [(2, 1, "Power"), (2, 2, "Brightness")],
    "ac_partner": [(2, 1, "Power")],
    "heater": [(2, 1, "Power"), (2, 2, "Target Temperature")],
    "fan": [(2, 1, "Power"), (2, 2, "Speed"), (2, 4, "Oscillate")],
    "lock": [(3, 1021, "Lock State"), (4, 1003, "Battery")],
    "kettle": [],
    "sensor_t9": [
        (3, 1001, "Temperature"),
        (3, 1002, "Humidity"),
        (2, 1003, "Battery"),
    ],
    "sensor_t2": [(2, 1, "Temperature"), (2, 2, "Humidity"), (3, 1, "Battery")],
}

VALUE_LABELS = {
    "Power": {True: "ON", False: "OFF"},
    "Lock State": {16: "Locked", 32: "Unlocked", 64: "Ajar"},
    "Oscillate": {True: "ON", False: "OFF"},
}

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


# ── Global State ────────────────────────────────────────

_session: Optional[aiohttp.ClientSession] = None
_auth: Optional[XiaomiAuth] = None
_devices: Optional[dict] = None
_devices_expire_at = 0


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
    matches = [(name, info) for name, info in _devices.items() if q in name.lower()]
    if matches:
        online = [(n, i) for n, i in matches if i["is_online"]]
        return (online or matches)[0]
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


# ── Commands ────────────────────────────────────────────


async def cmd_devices() -> str:
    devices = await _load_devices(force=True)
    lines = [f"Found {len(devices)} devices:\n"]
    for name, info in devices.items():
        status = "Online" if info["is_online"] else "Offline"
        lines.append(f"  {name} ({info['type']}) — {status}")
        lines.append(f"    Model: {info['model']} | DID: {info['did']}")
    return "\n".join(lines)


async def cmd_status(device_name: str = "") -> str:
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


async def cmd_control(device_name: str, action: str, value: str = "") -> str:
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


async def cmd_play(keyword: str = "") -> str:
    await _load_devices()
    speaker = _find_speaker()
    auth = await _get_auth()
    svc = XiaomiIOService(auth)
    text = f"播放{keyword}" if keyword else "播放音乐"
    result = await svc.miot_action(speaker["did"], [5, 1], [text])
    return f"XiaoAi: {text} (result: {result})"


async def cmd_pause() -> str:
    await _load_devices()
    auth = await _get_auth()
    svc = XiaomiIOService(auth)
    result = await svc.miot_action(_find_speaker()["did"], [3, 2], [])
    return f"Paused (result: {result})"


async def cmd_resume() -> str:
    await _load_devices()
    auth = await _get_auth()
    svc = XiaomiIOService(auth)
    result = await svc.miot_action(_find_speaker()["did"], [3, 1], [])
    return f"Resumed (result: {result})"


async def cmd_volume(volume: int) -> str:
    auth = await _get_auth()
    na = XiaomiNAService(auth)
    device_id = await _get_speaker_device_id()
    result = await na.player_set_volume(device_id, volume)
    return f"Volume set to {volume} (result: {result})"


async def cmd_tts(text: str) -> str:
    await _load_devices()
    auth = await _get_auth()
    svc = XiaomiIOService(auth)
    speaker = _find_speaker()
    result = await svc.miot_action(speaker["did"], [5, 1], [text])
    return f"XiaoAi speaking: '{text}' (result: {result})"


async def cmd_xiaoai(command: str) -> str:
    await _load_devices()
    auth = await _get_auth()
    svc = XiaomiIOService(auth)
    result = await svc.miot_action(_find_speaker()["did"], [5, 1], [command])
    return f"Sent to XiaoAi: '{command}' (result: {result})"


async def cmd_speaker() -> str:
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


# ── Main ────────────────────────────────────────────────


async def _run(cmd: str, args: list[str]) -> str:
    try:
        if cmd == "devices":
            return await cmd_devices()
        elif cmd == "status":
            return await cmd_status(args[0] if args else "")
        elif cmd == "control":
            if len(args) < 2:
                return "Usage: cli.py control <device_name> <action> [value]"
            return await cmd_control(args[0], args[1], args[2] if len(args) > 2 else "")
        elif cmd == "play":
            return await cmd_play(args[0] if args else "")
        elif cmd == "pause":
            return await cmd_pause()
        elif cmd == "resume":
            return await cmd_resume()
        elif cmd == "volume":
            if not args:
                return "Usage: cli.py volume <0-100>"
            return await cmd_volume(int(args[0]))
        elif cmd == "tts":
            if not args:
                return "Usage: cli.py tts <text>"
            return await cmd_tts(" ".join(args))
        elif cmd == "xiaoai":
            if not args:
                return "Usage: cli.py xiaoai <command>"
            return await cmd_xiaoai(" ".join(args))
        elif cmd == "speaker":
            return await cmd_speaker()
        else:
            return f"Unknown command: {cmd}\n\n{__doc__}"
    finally:
        if _session:
            await _session.close()


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__.strip())
        sys.exit(0)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    try:
        result = asyncio.run(_run(cmd, args))
        print(result)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
