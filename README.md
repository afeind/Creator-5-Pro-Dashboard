# Creator 5 Pro Dashboard

A self-hosted web dashboard for the FlashForge **Creator 5 Pro** 3D printer. Live status, temperatures, material station, chamber camera, and a working chamber-light toggle — served from a small Flask app in Docker, reachable from anywhere you can reach the container.

Built against firmware **1.9.4**. It talks to the printer's local HTTP API on port 8898 — no cloud account, no vendor app.

![status](https://img.shields.io/badge/firmware-1.9.4-blue) ![license](https://img.shields.io/badge/license-MIT-green)

---

## Features

- **Live job** — filename, thumbnail, percent, layer *n*/*N*, elapsed, remaining, speed, infill
- **Temperatures** — bed, chamber, and all four nozzles, with the active tool highlighted
- **Material station** — all 4 slots with their real filament colours, material names, and which is loaded
- **Chamber camera** — MJPEG stream, off by default, toggled on demand
- **Chamber light** — on/off, with the result verified against the printer (see [why that matters](#the-api-lies-about-success))
- **Lifetime totals** — print hours, filament used, free storage, firmware, door, fans, TVOC
- **Job history** — the firmware keeps no per-job log, so the app records completed jobs itself

---

## Quick start

```bash
git clone https://github.com/afeind/Creator-5-Pro-Dashboard.git
cd Creator-5-Pro-Dashboard

cp .env.example .env
$EDITOR .env          # <-- put your printer IP, serial, and access code here

docker compose up -d
```

Then open **http://localhost:5011**.

> **Where does the code go?** Everything you need to configure lives in **`.env`**, created by copying `.env.example`. That file is gitignored and is the only place credentials belong — nothing is hard-coded in the source. See [Configuration](#configuration).

---

## Finding your serial and access code

Two values are required, and they come from different places.

### Serial number — discoverable over the network

The printer answers a UDP discovery probe on port **48899** (or 19000) with **no authentication at all**. Send it any payload and it replies with its model name and serial:

```bash
python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
s.sendto(b'x', ('192.168.1.100', 48899))     # <-- your printer's IP
print(s.recvfrom(2048)[0])
"
```

You'll get back a fixed-width blob containing something like `Creator 5 Pro` and `SNXXXXXXXXXXX`. The `SN...` string is your serial.

### Access code — only on the printer's screen

The access code (the API calls it `checkCode`) is shown **only on the printer's own touchscreen**, under its network/connection settings. There is no way to retrieve it over the network — that's the point of it.

Sanity-check both values before starting the app:

```bash
curl -s -X POST http://192.168.1.100:8898/detail \
  -H 'Content-Type: application/json' \
  -d '{"serialNumber":"SNXXXXXXXXXXX","checkCode":"xxxxxxxx"}'
```

| Response | Meaning |
|---|---|
| `{"code":0,...}` plus a big JSON blob | Both correct |
| `{"code":1,"message":"Access code is different"}` | Serial fine, access code wrong |
| `{"code":-1,"message":"Parameters is error"}` | Malformed request / wrong serial |

---

## Configuration

| Variable | Required | Default | Notes |
|---|---|---|---|
| `PRINTER_HOST` | ✅ | `192.168.1.100` | Printer LAN IP |
| `PRINTER_SERIAL` | ✅ | — | `SN...`, see above |
| `PRINTER_CHECK_CODE` | ✅ | — | From the printer's touchscreen |
| `PRINTER_API_PORT` | | `8898` | HTTP API port |
| `PRINTER_CAM_PORT` | | `8080` | MJPEG camera port |
| `PRINTER_CAM_PATH` | | `/?action=stream` | Camera path |
| `DATA_DIR` | | `/data` | Where job history is written |

The container publishes port **5011**; change the mapping in `docker-compose.yml` if that clashes.

---

## Reverse proxy

To serve it behind a hostname, proxy to port 5011. **Disable response buffering**, or the camera stream will buffer forever and never render.

**Caddy**

```caddyfile
printer.example.com {
    reverse_proxy 127.0.0.1:5011 {
        flush_interval -1          # required for MJPEG
    }
}
```

**nginx**

```nginx
location / {
    proxy_pass http://127.0.0.1:5011;
    proxy_buffering off;           # required for MJPEG
    proxy_read_timeout 3600s;
}
```

This app has **no authentication of its own**. Anyone who can reach it can control your printer. Keep it on your LAN or behind your proxy's auth — don't expose it to the open internet.

---

## Notes on the printer's API

Undocumented behaviour discovered while building this. Recorded here because it cost real time and may save yours.

### The API lies about success

`POST /control` returns `{"code":0,"message":"Success"}` for **any** command — including ones that don't exist. Verified by sending `{"cmd":"totalNonsenseCmd"}` and getting `Success` back.

Consequence: **you cannot discover command names by trial and error.** Every wrong guess looks exactly like a right one. This app never trusts the reply — after sending a light command it re-reads `/detail` and reports what the printer actually says.

### The light command has a `_cmd` suffix

```jsonc
// works
{"cmd": "lightControl_cmd", "args": {"status": "open"}}   // or "close"

// silently does nothing, still returns Success
{"cmd": "lightControl",     "args": {"status": "open"}}
```

Meanwhile `streamCtrl` has **no** suffix. The naming is inconsistent between commands, so there's no rule to infer — these strings were recovered by packet-capturing the official desktop app.

### The camera is a one-shot

The MJPEG server at `:8080` refuses connections until you enable it:

```jsonc
{"cmd": "streamCtrl", "args": {"action": "open"}}
```

It then serves **exactly one client per enable** — when that client disconnects, the stream stops again. Re-enable before *every* connection and allow ~2–3 s for the encoder to come up. A browser holding one long-lived connection is fine; rapid connect/disconnect testing looks like random failure.

### `/detail` field quirks

| Field | Gotcha |
|---|---|
| `nozzleTemps` / `nozzleTargetTemps` | Arrays of 4 — there is no single `nozzleTemp`. Active tool = the one with target > 0 |
| `estimatedTime` | Time **remaining**, not job total. There is no `estimatedLeftTime` |
| `printProgress` | 0..1, not a percentage |
| `remainingDiskSpace` | **GB**, not MB |
| `cumulativePrintTime` | Minutes |
| `matlStationInfo.slotInfos[]` | Per-slot `materialName`, `materialColor` (hex), `hasFilament`; `currentSlot` marks the loaded one |

### Full API surface

`POST /detail`, `POST /control`, `POST /product`, `POST /uploadGcode`, `GET /getThum`. Everything else 404s. There is **no G-code passthrough**. All calls except `/getThum` require `{"serialNumber","checkCode"}` in the body.

---

## Endpoints

| Route | Purpose |
|---|---|
| `GET /` | Dashboard UI |
| `GET /api/status` | Normalised printer state |
| `GET /api/raw` | Raw `/detail` passthrough, for debugging |
| `POST /api/light` | `{"on": true\|false}` — verifies against the printer |
| `POST /api/camera` | `{"on": true\|false}` — enables/disables the stream |
| `GET /camera/stream` | Proxied MJPEG |
| `GET /camera/thumb` | Current job thumbnail |
| `GET /api/history` | Completed jobs recorded locally |
| `GET /healthz` | Health check |

---

## Compatibility

Developed and tested against a **Creator 5 Pro on firmware 1.9.4**. Other FlashForge models using the same port-8898 API (Adventurer 5M / 5M Pro and relatives) may work, but field names differ between single- and multi-nozzle firmware — the normaliser in `app.py` handles both shapes where it can. Reports welcome.

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with, endorsed by, or supported by FlashForge.
