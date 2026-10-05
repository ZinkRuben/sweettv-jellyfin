# sweettv-jellyfin

Watch your own [Sweet.TV](https://sweet.tv) subscription in [Jellyfin](https://jellyfin.org) Live TV.

Jellyfin's Live TV needs an M3U playlist and an XMLTV guide. Sweet.TV provides neither, and its stream URLs
are short-lived, so a static playlist doesn't work. This project is a small server that sits between the two:

| Endpoint | Use in Jellyfin |
|---|---|
| `/playlist.m3u` | **M3U Tuner** |
| `/epg.xml` | **XMLTV** guide provider |
| `/channel/<id>` | Redirects to a fresh HLS stream URL each time a channel is opened |

Pure Python 3, standard library only.

> **Unofficial.** This uses Sweet.TV's private API, which can change or break at any time, and may not be
> permitted by Sweet.TV's terms. Use it only with your own paid account, for personal use. Not affiliated with
> Sweet.TV or Jellyfin.

## Limitations

- **Plain HLS only.** The server redirects only to unencrypted HLS streams. Channels delivered as DRM-protected
  (Widevine) DASH are skipped and return an error. There is no DRM circumvention here and none is planned.
  `sweet_probe.py` tells you how many of your channels are plain HLS.
- **No authentication on the server.** Anyone who can reach the port can watch through your account. Keep it on
  your LAN or a VPN. Never expose it to the internet.
- **Stream limits.** Sweet.TV may limit concurrent streams per account.
- The guide is generated in UTC.

## Setup

### 1. Log in and check your channels

```bash
python3 sweet_probe.py --limit 10
```

It prints a code and a link. Open the link (`https://sweet.tv/addDevice?connectCode=<CODE>`) while logged in
to your account to confirm. Tokens are stored in `.sweet_state.json` (owner-only permissions) and refreshed
automatically.

Run it without `--limit` (optionally with `--csv results.csv`) to see how many channels come back as plain HLS.

### 2. Run the server

```bash
python3 sweet_server.py            # listens on 0.0.0.0:8099
python3 sweet_server.py --port 9000
```

### 3. Add it to Jellyfin

Dashboard → Live TV:

- **Tuner Devices → + → M3U Tuner**: `http://<server>:8099/playlist.m3u`
- **TV Guide Data Providers → + → XMLTV**: `http://<server>:8099/epg.xml`

Channels are numbered in Sweet.TV's order (`tvg-chno`). If channels aren't matched to the guide
automatically, map them by hand. Channel IDs match on both sides.

## Docker Compose

Copy `sweet_probe.py`, `sweet_server.py` and the logged-in `.sweet_state.json` into a config folder, then add:

```yaml
  sweettv:
    image: python:3.12-alpine
    container_name: sweettv
    working_dir: /config
    command: python3 sweet_server.py --host 0.0.0.0 --port 8099
    volumes:
      - /path/to/appdata/sweettv:/config
    ports:
      - 8099:8099
    restart: unless-stopped
```

The folder must be writable, because refreshed tokens are saved back to `.sweet_state.json`. Use
`http://sweettv:8099/...` in Jellyfin if both containers share a network, or `http://<host-ip>:8099/...` otherwise.

## If the login expires

The server refreshes tokens automatically and never starts a login by itself. If the refresh ever fails, requests
return an error telling you to run `python3 sweet_probe.py --limit 1` again and copy the new `.sweet_state.json`
to the server.

## How it works

The login flow, device profile and endpoints (`SigninService`, `AuthenticationService`, `TvService/GetChannels`,
`TvService/OpenStream`) follow what the Sweet.TV Kodi add-on
[plugin.video.sweettv](https://github.com/david-hazi/plugin.video.sweettv) does. Thanks to its author; this
project is an independent reimplementation for Jellyfin.

## Security

`.sweet_state.json` contains your account tokens. Keep it private and never commit it (it is in `.gitignore`).

## License

MIT, see [LICENSE](LICENSE).
