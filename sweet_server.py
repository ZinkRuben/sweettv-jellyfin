#!/usr/bin/env python3
"""Serve your Sweet.TV channels to Jellyfin as an M3U tuner plus an XMLTV guide.

    http://HOST:8099/playlist.m3u   -> Jellyfin "M3U Tuner"
    http://HOST:8099/epg.xml        -> Jellyfin "XMLTV" guide provider
    http://HOST:8099/channel/<id>   -> redirects to a fresh HLS stream URL

Stream URLs are not permanent, so the playlist points at this server and a
fresh URL is requested whenever a channel is opened.

Run `python3 sweet_probe.py --limit 1` once first to log in.
"""
import argparse
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import quoteattr

import sweet_probe as sp

LOCK = threading.Lock()
STATE = sp.load_state()
CACHE = {"channels": None, "categories": {}, "channels_at": 0, "epg": None, "epg_at": 0}
CHANNEL_TTL = 900
EPG_TTL = 1800


def api(path, payload):
    with LOCK:
        sp.ensure_token(STATE, interactive=False)
        token = STATE["access_token"]
    return sp.post(path, payload, token)


def fetch_channels(with_epg):
    res = api(
        "TvService/GetChannels.json",
        {
            "epg_limit_prev": 0,
            "epg_limit_next": 100 if with_epg else 0,
            "epg_current_time": int(time.time()),
            "need_epg": with_epg,
            "need_list": True,
            "need_categories": True,
            "need_offsets": False,
            "need_hash": False,
            "need_icons": False,
            "need_big_icons": False,
        },
    )
    if res.get("status") != "OK":
        raise RuntimeError("GetChannels failed: %s" % str(res)[:200])
    return res


def channels():
    if not CACHE["channels"] or time.time() - CACHE["channels_at"] > CHANNEL_TTL:
        res = fetch_channels(False)
        CACHE["channels"] = res.get("list", [])
        CACHE["categories"] = {c.get("id"): c.get("caption") for c in res.get("categories", [])}
        CACHE["channels_at"] = time.time()
    return CACHE["channels"], CACHE["categories"]


def group_of(ch, cats):
    for cid in ch.get("category", []):
        if cid != 1000 and cid in cats:
            return cats[cid]
    return ""


def build_playlist(base):
    chans, cats = channels()
    lines = ["#EXTM3U"]
    for n, ch in enumerate(chans, 1):
        lines.append(
            "#EXTINF:-1 tvg-chno=%s tvg-id=%s tvg-name=%s tvg-logo=%s group-title=%s,%s"
            % (
                quoteattr(str(n)),
                quoteattr(str(ch["id"])),
                quoteattr(ch.get("name", "")),
                quoteattr(ch.get("icon_v2_url") or ch.get("icon_url") or ""),
                quoteattr(group_of(ch, cats)),
                ch.get("name", ""),
            )
        )
        lines.append("%s/channel/%s" % (base, ch["id"]))
    return "\n".join(lines) + "\n"


def xmltv_time(ts):
    return time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(ts))


def build_epg():
    if CACHE["epg"] and time.time() - CACHE["epg_at"] < EPG_TTL:
        return CACHE["epg"]
    res = fetch_channels(True)
    root = ET.Element("tv", {"generator-info-name": "sweet_server"})
    for ch in res.get("list", []):
        c = ET.SubElement(root, "channel", {"id": str(ch["id"])})
        ET.SubElement(c, "display-name").text = ch.get("name", "")
        icon = ch.get("icon_v2_url") or ch.get("icon_url")
        if icon:
            ET.SubElement(c, "icon", {"src": icon})
    for ch in res.get("list", []):
        for ep in ch.get("epg") or []:
            if not ep.get("time_start") or not ep.get("time_stop"):
                continue
            p = ET.SubElement(
                root,
                "programme",
                {
                    "start": xmltv_time(ep["time_start"]),
                    "stop": xmltv_time(ep["time_stop"] - 1),
                    "channel": str(ch["id"]),
                },
            )
            ET.SubElement(p, "title").text = ep.get("text") or ep.get("title") or ""
            if ep.get("description"):
                ET.SubElement(p, "desc").text = ep["description"]
    data = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")
    CACHE["epg"], CACHE["epg_at"] = data, time.time()
    return data


def open_stream(channel_id):
    res = api(
        "TvService/OpenStream.json",
        {"without_auth": True, "channel_id": int(channel_id), "multistream": True},
    )
    if res.get("result") != "OK":
        raise RuntimeError("OpenStream: %s" % res.get("result"))
    if res.get("scheme") != "HTTP_HLS" or res.get("drm_type"):
        raise RuntimeError("channel is not plain HLS (%s %s)" % (res.get("scheme"), res.get("drm_type")))
    stream = res["http_stream"]
    return "https://%s%s" % (stream["host"]["address"], stream["url"])


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body=b"", ctype="text/plain; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        base = "http://%s" % self.headers.get("Host", "localhost")
        try:
            if path == "/playlist.m3u":
                self.reply(200, build_playlist(base), "audio/x-mpegurl; charset=utf-8")
            elif path == "/epg.xml":
                self.reply(200, build_epg(), "application/xml")
            elif path.startswith("/channel/"):
                url = open_stream(path.rsplit("/", 1)[1])
                self.reply(302, headers={"Location": url})
            else:
                self.reply(404, "not found")
        except Exception as e:  # keep serving after upstream errors
            self.reply(502, "error: %s" % e)

    def log_message(self, fmt, *args):
        print("%s %s" % (self.address_string(), fmt % args))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8099)
    args = ap.parse_args()
    if not STATE.get("access_token"):
        raise SystemExit("Not logged in. Run: python3 sweet_probe.py --limit 1")
    print("Serving on http://%s:%d  (playlist.m3u, epg.xml)" % (args.host, args.port), flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
