# Xiaomi Home MCP Server

[中文文档](README_CN.md)

Control your Xiaomi smart home devices through AI assistants via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/).

Works with [Claude Code](https://claude.ai/claude-code), [Claude Desktop](https://claude.ai/download), [Cursor](https://cursor.com), and any MCP-compatible client.

## Features

- **Device Discovery** — Automatically find all devices in your Xiaomi account
- **Status Monitoring** — Read power state, brightness, temperature, humidity, battery, lock status
- **Device Control** — Turn on/off, adjust brightness, color temperature, target temperature
- **XiaoAi Speaker** — Play/pause music, TTS, volume control, voice commands
- **QR Code Login** — Scan with Mi Home app, no password needed
- **Zero Dependencies Setup** — Just two Python files, no pip install required

## Quick Start

### Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Mi Home app on your phone (for QR code login)

### 1. Login

```bash
uv run --script login.py
```

A QR code will open in your browser. Scan it with the Mi Home app (Profile → Scan icon in top-right corner).

Tokens are saved to `~/.xiaomi-mcp/tokens.json` with `600` permissions.

### 2. Connect to Claude Code

```bash
claude mcp add xiaomi-home -- uv run --script /path/to/mcp_server.py
```

That's it! Now you can talk to your devices:

> "Turn on the lamp"
> "What's the room temperature?"
> "Is the door locked?"
> "Set brightness to 50%"
> "Play some music"

### 2a. Connect to Claude Desktop

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "xiaomi-home": {
      "command": "uv",
      "args": ["run", "--script", "/path/to/mcp_server.py"]
    }
  }
}
```

## Available Tools

| Tool | Description |
|------|-------------|
| `get_devices()` | List all devices and online status |
| `get_device_status(name?)` | Query detailed status (power, brightness, temperature, battery, etc.) |
| `control_device(name, action)` | Control device: `on`/`off`, `brightness`, `color_temp`, `target_temp` |
| `play_music(keyword?)` | Play music on XiaoAi speaker |
| `pause_music()` | Pause playback |
| `resume_music()` | Resume playback |
| `set_volume(volume)` | Set speaker volume (0-100) |
| `tts(text)` | Text-to-speech on speaker |
| `xiaoai_command(command)` | Send voice command to XiaoAi |
| `get_speaker_status()` | Get playback status |

## Supported Devices

The server auto-detects device types by model prefix:

| Type | Models | Status Query | Control |
|------|--------|-------------|---------|
| Light | `philips.light.*`, `yeelink.light.*` | Power, Brightness | On/Off, Brightness, Color Temp |
| AC Partner | `lumi.acpartner.*` | Power | On/Off |
| Heater | `xiaomi.heater.*` | Power, Target Temp | On/Off, Target Temp |
| Fan | `xiaomi.fan.*` | Power, Speed, Oscillate | On/Off |
| Lock | `loock.lock.*` | Lock State, Battery | — |
| Sensor | `miaomiaoce.sensor_ht.*` | Temperature, Humidity, Battery | — |
| Speaker | `xiaomi.wifispeaker.*` | Playback Status | Play, Pause, Volume, TTS |
| Kettle | `yunmi.kettle.*` | BLE only (not supported) | — |

> **Note:** MIoT spec properties (siid/piid) vary by model. The built-in mappings cover common models. If your device returns incorrect values, you may need to look up the correct spec at [miot-spec.com](https://home.miot-spec.com/) and adjust the `MIOT_PROPS` dict in `mcp_server.py`.

## Token Management

```bash
# Check if token is still valid
uv run --script login.py --check

# Clear cached token
uv run --script login.py --logout

# Re-login
uv run --script login.py
```

Tokens are long-lived but may expire after weeks/months. If the MCP server starts failing, re-run `login.py`.

## Alternative: Environment Variables

If you prefer not to use the login tool, you can pass credentials directly:

```bash
claude mcp add xiaomi-home \
    -e XIAOMI_PASS_TOKEN="your_pass_token" \
    -e XIAOMI_USER_ID="your_user_id" \
    -e XIAOMI_DEVICE_ID="your_device_id" \
    -- uv run --script /path/to/mcp_server.py
```

To get these values, log in to [account.xiaomi.com](https://account.xiaomi.com) in your browser and extract `passToken`, `userId`, and `deviceId` from cookies.

## Use with AI Agents (OpenClaw, etc.)

If your AI agent can run shell commands, it can control Xiaomi devices directly via CLI — no MCP needed.

### For AI Agents: Setup Instructions

> **AI agent**: Follow these steps to set up Xiaomi smart home control for the user.

**Step 1.** Clone this repository:
```bash
git clone https://github.com/gehaiyi/xiaomi-home-mcp.git
cd xiaomi-home-mcp
```

**Step 2.** Ask the user to run the login script and scan the QR code with the Mi Home app (Profile → Scan icon):
```bash
uv run --script login.py
```

**Step 3.** Verify login succeeded:
```bash
uv run --script login.py --check
```

**Step 4.** You can now control devices using `cli.py`. All commands below use the format:
```bash
uv run --script /path/to/xiaomi-home-mcp/cli.py <command> [args...]
```

### CLI Commands

| Command | Description | Example |
|---------|-------------|---------|
| `devices` | List all devices and online status | `cli.py devices` |
| `status [name]` | Query device status (all online if no name) | `cli.py status "lamp"` |
| `control <name> <action> [value]` | Control a device | `cli.py control "lamp" on` |
| `play [keyword]` | Play music on XiaoAi speaker | `cli.py play "Jay Chou"` |
| `pause` | Pause playback | `cli.py pause` |
| `resume` | Resume playback | `cli.py resume` |
| `volume <0-100>` | Set speaker volume | `cli.py volume 30` |
| `tts <text>` | Text-to-speech on speaker | `cli.py tts "Dinner is ready"` |
| `xiaoai <command>` | Send voice command to XiaoAi | `cli.py xiaoai "What's the weather"` |
| `speaker` | Get playback status | `cli.py speaker` |

**Control actions:** `on`, `off`, `brightness <1-100>`, `color_temp <2700-6500>`, `target_temp <16-30>`

**Device name matching:** Supports fuzzy match — `"lamp"` matches `"Bedroom Lamp"`. Chinese keywords like `"灯"`, `"空调"`, `"音箱"` also work.

## How It Works

```
Mi Home App (scan QR) → login.py → ~/.xiaomi-mcp/tokens.json
                                          ↓
Claude / Cursor ←→ MCP Protocol ←→ mcp_server.py ←→ Xiaomi Cloud API
                                                      (api.io.mi.com)
```

- **Authentication**: passToken → serviceToken exchange (auto-refreshed)
- **Device Control**: MIoT spec protocol (siid/piid) + legacy miio RPC
- **Speaker**: Dual channel — IoT (control/TTS) + MiNA (volume/status)

## License

MIT
