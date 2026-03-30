# Xiaomi Home MCP Server

通过 [MCP 协议](https://modelcontextprotocol.io/) 让 AI 助手控制你的小米智能家居设备。

支持 [Claude Code](https://claude.ai/claude-code)、[Claude Desktop](https://claude.ai/download)、[Cursor](https://cursor.com) 等所有 MCP 兼容客户端。

## 功能

- **设备发现** — 自动获取小米账号下所有设备
- **状态监控** — 读取开关、亮度、温湿度、电量、门锁状态
- **设备控制** — 开关、调亮度、调色温、调温度
- **小爱音箱** — 播放/暂停音乐、语音播报、音量控制、语音指令
- **扫码登录** — 米家 App 扫一下，不用输密码
- **零配置** — 两个 Python 文件，不需要 pip install

## 快速开始

### 前置条件

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)（推荐）或 pip
- 手机上的米家 App（扫码登录用）

### 1. 登录

```bash
uv run --script login.py
```

浏览器会弹出一个二维码，用米家 App 扫描（我的 → 右上角扫一扫）。

Token 保存在 `~/.xiaomi-mcp/tokens.json`，权限 600 仅本人可读。

### 2. 接入 Claude Code

```bash
claude mcp add xiaomi-home -- uv run --script /path/to/mcp_server.py
```

搞定！现在可以直接对话控制设备：

> "开灯"
> "现在室温多少度？"
> "门锁了吗？"
> "亮度调到 50%"
> "放首歌"

### 2a. 接入 Claude Desktop

在 Claude Desktop 配置文件中添加（`~/Library/Application Support/Claude/claude_desktop_config.json`）：

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

## 可用工具

| 工具 | 说明 |
|------|------|
| `get_devices()` | 列出所有设备及在线状态 |
| `get_device_status(name?)` | 查询设备详细状态（开关、亮度、温度、电量等） |
| `control_device(name, action)` | 控制设备：`on`/`off`、`brightness`、`color_temp`、`target_temp` |
| `play_music(keyword?)` | 小爱音箱播放音乐 |
| `pause_music()` | 暂停播放 |
| `resume_music()` | 继续播放 |
| `set_volume(volume)` | 设置音量 (0-100) |
| `tts(text)` | 语音播报 |
| `xiaoai_command(command)` | 向小爱发送语音指令 |
| `get_speaker_status()` | 获取播放状态 |

## 支持的设备

服务器通过 model 前缀自动识别设备类型：

| 类型 | 型号 | 状态查询 | 控制 |
|------|------|---------|------|
| 灯 | `philips.light.*`, `yeelink.light.*` | 开关、亮度 | 开关、亮度、色温 |
| 空调伴侣 | `lumi.acpartner.*` | 开关 | 开关 |
| 电暖器 | `xiaomi.heater.*` | 开关、目标温度 | 开关、目标温度 |
| 风扇 | `xiaomi.fan.*` | 开关、档位、摇头 | 开关 |
| 门锁 | `loock.lock.*` | 锁状态、电量 | — |
| 温湿度计 | `miaomiaoce.sensor_ht.*` | 温度、湿度、电量 | — |
| 小爱音箱 | `xiaomi.wifispeaker.*` | 播放状态 | 播放、暂停、音量、TTS |
| 电水壶 | `yunmi.kettle.*` | 仅 BLE（不支持） | — |

> **注意：** MIoT spec 属性 (siid/piid) 因型号而异。内置映射覆盖了常见型号。如果你的设备返回值不对，可以到 [miot-spec.com](https://home.miot-spec.com/) 查询正确的 spec，然后修改 `mcp_server.py` 中的 `MIOT_PROPS`。

## Token 管理

```bash
# 检查 token 是否有效
uv run --script login.py --check

# 清除 token
uv run --script login.py --logout

# 重新登录
uv run --script login.py
```

Token 有效期较长，但可能在数周/数月后过期。如果 MCP Server 开始报错，重新跑一次 `login.py` 即可。

## 备选方案：环境变量

如果不想用扫码登录，也可以直接传环境变量：

```bash
claude mcp add xiaomi-home \
    -e XIAOMI_PASS_TOKEN="your_pass_token" \
    -e XIAOMI_USER_ID="your_user_id" \
    -e XIAOMI_DEVICE_ID="your_device_id" \
    -- uv run --script /path/to/mcp_server.py
```

这些值需要在浏览器登录 [account.xiaomi.com](https://account.xiaomi.com) 后从 cookie 中提取。

## 工作原理

```
米家 App (扫码) → login.py → ~/.xiaomi-mcp/tokens.json
                                      ↓
Claude / Cursor ←→ MCP 协议 ←→ mcp_server.py ←→ 小米云端 API
                                                  (api.io.mi.com)
```

- **认证**：passToken → serviceToken 自动换取（自动刷新）
- **设备控制**：MIoT spec 协议 (siid/piid) + 旧版 miio RPC
- **音箱**：双通道 — IoT（控制/TTS）+ MiNA（音量/状态）

## License

MIT
