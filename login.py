#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["aiohttp"]
# ///
"""
Xiaomi Account Login — Authenticate via QR code and cache tokens locally.

Usage:
    uv run --script login.py           # QR code login
    uv run --script login.py --check   # Check if token is valid
    uv run --script login.py --logout  # Clear cached token

Tokens are saved to ~/.xiaomi-mcp/tokens.json and automatically loaded by the MCP server.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import webbrowser
from base64 import b64encode
from hashlib import md5, sha1
from pathlib import Path
from urllib.parse import quote, urlencode

import aiohttp

# ── Constants ──────────────────────────────────────────

TOKEN_DIR = Path.home() / ".xiaomi-mcp"
TOKEN_FILE = TOKEN_DIR / "tokens.json"
BASE_URL = "https://account.xiaomi.com/pass"
UA = "APP/com.xiaomi.mihome APPV/6.0.103 iosPassportSDK/3.9.0 iOS/14.4 miHSTS"
SID = "xiaomiio"


# ── Token Storage ──────────────────────────────────────


def load_tokens() -> dict | None:
    """Load cached tokens from disk."""
    if TOKEN_FILE.exists():
        try:
            data = json.loads(TOKEN_FILE.read_text())
            if data.get("userId") and data.get("passToken"):
                return data
        except (json.JSONDecodeError, KeyError):
            pass
    return None


def save_tokens(tokens: dict):
    """Save tokens to disk with restricted permissions."""
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2, ensure_ascii=False))
    TOKEN_FILE.chmod(0o600)
    print(f"Token saved to {TOKEN_FILE}")


def clear_tokens():
    """Remove cached tokens."""
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
        print("Token cleared")
    else:
        print("No cached token found")


# ── Xiaomi QR Code Login ──────────────────────────────


def _parse_json(text: str) -> dict:
    """Parse Xiaomi API response (strip &&&START&&& prefix)."""
    if text.startswith("&&&START&&&"):
        text = text[11:]
    return json.loads(text)


async def login_qrcode():
    """Login to Xiaomi account via QR code scan."""
    print("Xiaomi Account Login (QR Code)\n")
    print("Scan the QR code with Mi Home app to login.\n")

    device_id = md5(f"xiaomi-mcp-qr-{int(time.time())}".encode()).hexdigest()[:16]

    async with aiohttp.ClientSession() as session:
        # Step 1: Get QR code
        print("[1/3] Fetching QR code...")
        qr_params = urlencode(
            {
                "_qrsize": "480",
                "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
                "callback": "https://sts.api.io.mi.com/sts",
                "_hasLogo": "false",
                "sid": SID,
                "serviceParam": "",
                "_locale": "zh_CN",
                "_dc": str(int(time.time() * 1000)),
            },
            quote_via=quote,
        )
        async with session.get(
            f"https://account.xiaomi.com/longPolling/loginUrl?{qr_params}",
            headers={"User-Agent": UA},
            cookies={"sdkVersion": "accountsdk-18.8.15", "deviceId": device_id},
        ) as resp:
            text = (await resp.read()).decode("utf-8")
            try:
                result = _parse_json(text)
            except json.JSONDecodeError:
                print(f"Error: unexpected response:\n{text[:500]}")
                return

        qr_url = result.get("qr")
        lp_url = result.get("lp")
        timeout = result.get("timeout", 300)

        if not qr_url or not lp_url:
            print(f"Error: failed to get QR code: {result}")
            return

        # Download and open QR code image
        async with session.get(qr_url) as resp:
            img_data = await resp.read()
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tmp.write(img_data)
            tmp.close()
            webbrowser.open(f"file://{tmp.name}")
            print(f"  QR code opened: {tmp.name}")

        # Step 2: Wait for scan
        print("[2/3] Waiting for scan... (Mi Home app -> Profile -> Scan)")
        start_time = time.time()
        login_result = None

        while time.time() - start_time < timeout:
            try:
                async with session.get(
                    lp_url,
                    headers={"User-Agent": UA},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        text = (await resp.read()).decode("utf-8")
                        login_result = _parse_json(text)
                        if login_result.get("passToken"):
                            break
                        lp_url_new = login_result.get("lp")
                        if lp_url_new:
                            lp_url = lp_url_new
            except asyncio.TimeoutError:
                elapsed = int(time.time() - start_time)
                remaining = timeout - elapsed
                if remaining > 0:
                    print(f"  Waiting... ({remaining}s remaining)")
                continue
            except Exception:
                continue

        # Clean up QR code image
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

        if not login_result or not login_result.get("passToken"):
            print("Error: scan timed out, please try again")
            return

        print("  Scan successful!")

        # Step 3: Get serviceToken
        print("[3/3] Getting serviceToken...")
        location = login_result["location"]
        nonce = login_result["nonce"]
        ssecurity = login_result["ssecurity"]
        pass_token = login_result["passToken"]
        user_id = str(login_result["userId"])

        nsec = f"nonce={nonce}&{ssecurity}"
        client_sign = b64encode(sha1(nsec.encode()).digest()).decode()
        url = f"{location}&clientSign={quote(client_sign)}"

        async with session.get(
            url, headers={"User-Agent": "APP/com.xiaomi.mihome"}
        ) as resp:
            service_token = None
            if "serviceToken" in resp.cookies:
                service_token = resp.cookies["serviceToken"].value

        if not service_token:
            print("Error: failed to get serviceToken")
            return

        tokens = {
            "userId": user_id,
            "passToken": pass_token,
            "deviceId": device_id,
            "ssecurity": ssecurity,
        }
        save_tokens(tokens)

        print(f"\nLogin successful!")
        print(f"  User ID: {user_id}")
        print(f"  The MCP server will automatically use these tokens.")


async def check_tokens():
    """Check if cached tokens are still valid."""
    tokens = load_tokens()
    if not tokens:
        print("No cached token found. Run `uv run --script login.py` to login.")
        return

    print(f"Token file: {TOKEN_FILE}")
    print(f"User ID:    {tokens['userId']}")

    async with aiohttp.ClientSession() as session:
        cookies = {
            "sdkVersion": "3.9",
            "deviceId": tokens["deviceId"],
            "userId": tokens["userId"],
            "passToken": tokens["passToken"],
        }
        async with session.get(
            f"{BASE_URL}/serviceLogin?sid={SID}&_json=true",
            headers={"User-Agent": UA},
            cookies=cookies,
        ) as resp:
            text = (await resp.read()).decode("utf-8")
            try:
                result = _parse_json(text)
            except json.JSONDecodeError:
                print("Token expired. Run `uv run --script login.py` to re-login.")
                return

        if result.get("ssecurity") and result.get("location"):
            print("Token is valid")
        else:
            print("Token expired. Run `uv run --script login.py` to re-login.")


# ── Main ───────────────────────────────────────────────


def main():
    if "--check" in sys.argv:
        asyncio.run(check_tokens())
    elif "--logout" in sys.argv:
        clear_tokens()
    elif "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
    else:
        asyncio.run(login_qrcode())


if __name__ == "__main__":
    main()
