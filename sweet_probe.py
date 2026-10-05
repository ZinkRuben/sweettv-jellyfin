#!/usr/bin/env python3
"""Check which Sweet.TV channels come back as plain HLS and which as DRM.

Logs in with the device-code flow, lists your channels, asks the API to open
each one and records the stream scheme (HTTP_HLS / HTTP_DASH) and DRM type.
It never fetches or saves any video; it only reads the metadata of the
OpenStream response.

Usage:
    python3 sweet_probe.py            # log in (first run) and probe all channels
    python3 sweet_probe.py --limit 20 # probe only the first 20 channels
    python3 sweet_probe.py --csv out.csv
"""
import argparse
import csv
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter

API = "https://api.sweet.tv/{}"
VERSION = "3.7.1"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sweet_state.json")


class ApiError(Exception):
    pass


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    fd = os.open(STATE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)


def device_profile(state):
    if "uuid" not in state:
        state["uuid"] = str(uuid.uuid4())
        state["mac"] = ":".join("%02x" % random.randint(0, 255) for _ in range(6))
    return {
        "device": {
            "type": "DT_AndroidTV",
            "mac": state["mac"],
            "firmware": {"versionCode": 1301, "versionString": VERSION, "modules": []},
            "sub_type": "DST_Unknown",
            "uuid": state["uuid"],
            "screen_info": {"width": 1920, "height": 1080, "aspectRatio": "AR_16_9"},
            "application": {"type": "AT_SWEET_TV_Player"},
            "vendor": "unknown",
            "supported_drm": {
                "aes128": False,
                "widevineModular": True,
                "widevineClassic": False,
                "playReady": False,
                "fairPlay": False,
            },
            "guid": "",
            "system": "",
            "system_info": {
                "network_connection_type": "NCT_Unknown",
                "os_version": "23",
                "total_memory": 2,
                "hardware": "",
            },
            "advertisingId": str(uuid.uuid4()),
        }
    }


def post(path, payload, token=None):
    headers = {
        "user-agent": UA,
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "accept-language": "en",
        "x-device": "1;22;0;2;" + VERSION,
        "origin": "https://sweet.tv",
        "referer": "https://sweet.tv/",
    }
    if token:
        headers["authorization"] = "Bearer " + token
    req = urllib.request.Request(
        API.format(path), data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise ApiError("HTTP %s from %s: %s" % (e.code, path, body))


def login(state):
    start = post("SigninService/Start.json", device_profile(state))
    code = start.get("auth_code")
    if not code:
        raise SystemExit("No auth_code in response: %s" % json.dumps(start)[:300])
    print("Login code:", code)
    extra = {k: v for k, v in start.items() if k != "auth_code"}
    if extra:
        print("Server also returned:", json.dumps(extra)[:300])
    print("\nOpen this link while logged in to your Sweet.TV account to confirm:")
    print("  https://sweet.tv/addDevice?connectCode=%s\n" % code)
    print("(or go to https://sweet.tv/addDevice and enter the code manually)")
    print("Waiting for confirmation (Ctrl+C to abort)...")
    deadline = time.time() + 300
    while time.time() < deadline:
        res = post("SigninService/GetStatus.json", {"auth_code": code})
        if res.get("result") == "COMPLETED":
            state["access_token"] = res["access_token"]
            state["refresh_token"] = res.get("refresh_token")
            state["expires_in"] = int(res.get("expires_in", 3600))
            state["token_time"] = int(time.time())
            save_state(state)
            print("Logged in.")
            return
        time.sleep(3)
    raise SystemExit("Timed out waiting for confirmation.")


def refresh(state):
    payload = device_profile(state)
    payload["refresh_token"] = state["refresh_token"]
    res = post("AuthenticationService/Token.json", payload)
    if not res.get("access_token"):
        return False
    state["access_token"] = res["access_token"]
    state["refresh_token"] = res.get("refresh_token", state["refresh_token"])
    state["expires_in"] = int(res.get("expires_in", state.get("expires_in", 3600)))
    state["token_time"] = int(time.time())
    save_state(state)
    return True


def ensure_token(state, interactive=True):
    """Refresh the access token when due. Without `interactive`, never start a
    new login: raise ApiError so servers can fail fast instead of blocking."""
    if not state.get("access_token"):
        if not interactive:
            raise ApiError("Not logged in. Run: python3 sweet_probe.py --limit 1")
        login(state)
        return
    age = int(time.time()) - state.get("token_time", 0)
    if age > state.get("expires_in", 3600) * 0.9:
        try:
            ok = bool(state.get("refresh_token")) and refresh(state)
        except ApiError:
            ok = False
        if not ok:
            if not interactive:
                raise ApiError(
                    "Sweet.TV login expired and could not be refreshed. "
                    "Run `python3 sweet_probe.py --limit 1` and copy the new .sweet_state.json."
                )
            login(state)


def get_channels(state):
    res = post(
        "TvService/GetChannels.json",
        {
            "epg_limit_prev": 0,
            "epg_limit_next": 0,
            "epg_current_time": int(time.time()),
            "need_epg": False,
            "need_list": True,
            "need_categories": False,
            "need_offsets": False,
            "need_hash": False,
            "need_icons": False,
            "need_big_icons": False,
        },
        state["access_token"],
    )
    if res.get("status") != "OK":
        raise SystemExit("GetChannels failed: %s" % json.dumps(res)[:300])
    return res.get("list", [])


def probe(state, channel_id):
    res = post(
        "TvService/OpenStream.json",
        {"without_auth": True, "channel_id": int(channel_id), "multistream": True},
        state["access_token"],
    )
    if res.get("result") != "OK":
        return "ERROR:%s" % res.get("result", "?"), ""
    return res.get("scheme", "?"), res.get("drm_type", "") or ""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=0, help="probe only the first N channels")
    ap.add_argument("--delay", type=float, default=0.5, help="seconds between requests")
    ap.add_argument("--csv", help="write per-channel results to this CSV file")
    args = ap.parse_args()

    state = load_state()
    ensure_token(state)

    channels = get_channels(state)
    if args.limit:
        channels = channels[: args.limit]
    print("Probing %d channels...\n" % len(channels))

    rows, tally = [], Counter()
    for i, ch in enumerate(channels, 1):
        ensure_token(state)
        scheme, drm = probe(state, ch["id"])
        label = scheme + (" + " + drm if drm else "")
        tally[label] += 1
        rows.append((ch["id"], ch.get("name", ""), scheme, drm))
        print("%3d/%d  %-32s %s" % (i, len(channels), (ch.get("name") or "")[:32], label))
        time.sleep(args.delay)

    print("\nSummary")
    for label, n in tally.most_common():
        print("  %4d  %s" % (n, label))
    plain = sum(n for label, n in tally.items() if label == "HTTP_HLS")
    print("\nPlayable in Jellyfin without DRM: %d of %d" % (plain, len(channels)))

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["id", "name", "scheme", "drm_type"])
            w.writerows(rows)
        print("Wrote", args.csv)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
    except ApiError as e:
        sys.exit(str(e))
