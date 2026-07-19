"""Creator 5 Pro dashboard — status, light control, camera, job totals."""
import json
import os
import threading
import time

import requests
from flask import Flask, Response, jsonify, render_template, request

PRINTER_HOST = os.environ.get("PRINTER_HOST", "192.168.1.100")
API_PORT = int(os.environ.get("PRINTER_API_PORT", "8898"))
CAM_PORT = int(os.environ.get("PRINTER_CAM_PORT", "8080"))
CAM_PATH = os.environ.get("PRINTER_CAM_PATH", "/?action=stream")
SERIAL = os.environ.get("PRINTER_SERIAL", "")
CHECK_CODE = os.environ.get("PRINTER_CHECK_CODE", "")
DATA_DIR = os.environ.get("DATA_DIR", "/data")

BASE = f"http://{PRINTER_HOST}:{API_PORT}"
CAM_URL = f"http://{PRINTER_HOST}:{CAM_PORT}{CAM_PATH}"
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")

app = Flask(__name__)

# Cache the last good poll so the UI degrades gracefully if the printer sleeps.
_cache = {"detail": None, "ts": 0, "error": None}
_lock = threading.Lock()


def ff_post(path, extra=None, timeout=8):
    body = {"serialNumber": SERIAL, "checkCode": CHECK_CODE}
    if extra:
        body.update(extra)
    r = requests.post(BASE + path, json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_detail():
    """Return (detail_dict, error_string)."""
    try:
        data = ff_post("/detail")
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if data.get("code") != 0:
        return None, data.get("message", "unknown printer error")
    # Firmware nests the payload under a couple of different keys.
    detail = data.get("detail") or data.get("data") or {}
    if isinstance(detail, dict) and "detail" in detail:
        detail = detail["detail"]
    return detail, None


def pick(d, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def to_float(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def normalize(d):
    """Map raw firmware fields onto a stable shape for the UI."""
    if not d:
        return {}

    progress = to_float(pick(d, "printProgress", "progress"), 0.0) or 0.0
    if progress <= 1.0:  # firmware reports 0..1
        progress *= 100.0

    status = str(pick(d, "status", "machineStatus", default="unknown")).lower()
    light = str(pick(d, "lightStatus", "led", default="")).lower()

    # This machine is a 4-nozzle tool-changer: temps arrive as parallel arrays,
    # not the single nozzleTemp/nozzleTargetTemp of single-extruder firmware.
    temps_arr = d.get("nozzleTemps") or []
    targs_arr = d.get("nozzleTargetTemps") or []
    if not temps_arr and pick(d, "nozzleTemp") is not None:
        temps_arr = [to_float(pick(d, "nozzleTemp"), 0)]
        targs_arr = [to_float(pick(d, "nozzleTargetTemp"), 0)]
    nozzles = []
    for i, t in enumerate(temps_arr):
        nozzles.append({
            "id": i + 1,
            "temp": to_float(t, 0) or 0,
            "target": to_float(targs_arr[i] if i < len(targs_arr) else 0, 0) or 0,
        })
    # The tool in use is the one with a live target; fall back to the hottest.
    active = None
    heated = [n for n in nozzles if n["target"] > 0]
    if heated:
        active = max(heated, key=lambda n: n["target"])
    elif nozzles:
        active = max(nozzles, key=lambda n: n["temp"])
    active = active or {"id": 0, "temp": 0, "target": 0}

    # Material station (4-slot AMS-style feeder).
    ms = d.get("matlStationInfo") or {}
    slots = []
    for s in (ms.get("slotInfos") or []):
        slots.append({
            "id": s.get("slotId"),
            "material": s.get("materialName") or "",
            "color": s.get("materialColor") or "",
            "loaded": bool(s.get("hasFilament")),
            "active": s.get("slotId") == ms.get("currentSlot"),
        })

    # estimatedTime is time REMAINING, not job total (verified against
    # printDuration vs printProgress on a live job).
    elapsed = to_float(pick(d, "printDuration"), 0) or 0
    remaining = to_float(pick(d, "estimatedTime"), 0) or 0

    return {
        "name": pick(d, "name", "machineName", default="Creator 5 Pro"),
        "model": pick(d, "model", default=""),
        "status": status,
        "printing": status in ("printing", "busy", "heating"),
        "error_code": pick(d, "errorCode", default=""),
        "firmware": pick(d, "firmwareVersion", default=""),
        "ip": pick(d, "ipAddr", default=PRINTER_HOST),
        "mac": pick(d, "macAddr", default=""),
        "location": pick(d, "location", default=""),
        "light_on": light in ("open", "on", "1", "true"),
        "has_camera": bool(d.get("camera")),
        "job": {
            "file": pick(d, "printFileName", "fileName", default=""),
            "has_thumb": bool(pick(d, "printFileThumbUrl", default="")),
            "progress": round(progress, 1),
            "layer": to_float(pick(d, "printLayer"), 0),
            "layer_total": to_float(pick(d, "targetPrintLayer"), 0),
            "elapsed_s": elapsed,
            "remaining_s": remaining,
            "total_s": elapsed + remaining,
            "speed_mm_s": to_float(pick(d, "currentPrintSpeed"), 0),
            "speed_pct": to_float(pick(d, "printSpeedAdjust"), 0),
            "fill": to_float(pick(d, "fillAmount"), 0),
        },
        "nozzles": nozzles,
        "temps": {
            "nozzle": active["temp"],
            "nozzle_target": active["target"],
            "nozzle_id": active["id"],
            "bed": to_float(pick(d, "platTemp"), 0),
            "bed_target": to_float(pick(d, "platTargetTemp"), 0),
            "chamber": to_float(pick(d, "chamberTemp"), 0),
            "chamber_target": to_float(pick(d, "chamberTargetTemp"), 0),
        },
        "material_station": {
            "slots": slots,
            "slot_count": ms.get("slotCnt") or len(slots),
            "current_slot": ms.get("currentSlot"),
        },
        "filament": {
            "left_type": pick(d, "leftFilamentType", default=""),
            "right_type": pick(d, "rightFilamentType", default=""),
            "est_len_m": to_float(pick(d, "estimatedRightLen"), 0),
            "est_weight_g": to_float(pick(d, "estimatedRightWeight"), 0),
            "cumulative_m": to_float(pick(d, "cumulativeFilament"), 0),
        },
        "fans": {
            "cooling": to_float(pick(d, "coolingFanSpeed"), 0),
            "chamber": to_float(pick(d, "chamberFanSpeed"), 0),
            "internal": pick(d, "internalFanStatus", default=""),
            "external": pick(d, "externalFanStatus", default=""),
        },
        "machine": {
            "nozzle_model": pick(d, "nozzleModel", default=""),
            "nozzle_count": pick(d, "nozzleCnt", default=""),
            "door": pick(d, "doorStatus", default=""),
            "tvoc": to_float(pick(d, "tvoc"), 0),
            "disk_free_gb": to_float(pick(d, "remainingDiskSpace"), 0),
            # cumulativePrintTime is reported in minutes.
            "cumulative_print_s": (to_float(pick(d, "cumulativePrintTime"), 0) or 0) * 60,
            "measure": pick(d, "measure", default=""),
            "lidar": bool(d.get("lidar")),
        },
    }


# --- local job history (the firmware exposes lifetime totals but no job log) ---

def load_history():
    try:
        with open(HISTORY_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {"jobs": []}


def save_history(hist):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(hist, fh)
        os.replace(tmp, HISTORY_FILE)
    except Exception:
        pass


def poller():
    """Background poll: keeps the cache warm and logs finished jobs."""
    last_file, last_printing = None, False
    while True:
        detail, err = fetch_detail()
        with _lock:
            if detail is not None:
                _cache.update(detail=detail, ts=time.time(), error=None)
            else:
                _cache["error"] = err

        if detail:
            n = normalize(detail)
            printing = n["printing"]
            fname = n["job"]["file"]
            # printing -> not printing with high progress == a completed job
            if last_printing and not printing and last_file:
                hist = load_history()
                hist["jobs"].append({
                    "file": last_file,
                    "finished_at": int(time.time()),
                    "duration_s": n["job"]["elapsed_s"],
                    "status": n["status"],
                })
                hist["jobs"] = hist["jobs"][-500:]
                save_history(hist)
            last_printing = printing
            if printing:
                last_file = fname
        time.sleep(10)


@app.route("/")
def index():
    return render_template("index.html", printer_host=PRINTER_HOST,
                           configured=bool(SERIAL and CHECK_CODE))


@app.route("/api/status")
def api_status():
    detail, err = fetch_detail()
    if detail is None:
        with _lock:
            stale = _cache.get("detail")
            age = time.time() - _cache.get("ts", 0)
        if stale:
            out = normalize(stale)
            out["_stale"] = True
            out["_age_s"] = int(age)
            out["_error"] = err
            return jsonify(out)
        return jsonify({"_error": err or "no data"}), 502
    out = normalize(detail)
    out["_stale"] = False
    return jsonify(out)


@app.route("/api/raw")
def api_raw():
    detail, err = fetch_detail()
    return jsonify({"detail": detail, "error": err})


def ff_control(cmd, args):
    """Send a control command.

    Command names are exactly as the FlashForge desktop app sends them —
    captured off the wire. Note `lightControl_cmd` carries a `_cmd` suffix
    while `streamCtrl` does not; the firmware silently ignores anything it
    doesn't recognise (and still answers "Success"), so these strings are
    load-bearing. Don't "tidy" them.
    """
    try:
        res = ff_post("/control", {"payload": {"cmd": cmd, "args": args}})
        return res.get("code") == 0, {"res": res}
    except Exception as exc:
        return False, {"error": f"{type(exc).__name__}: {exc}"}


@app.route("/api/light", methods=["POST"])
def api_light():
    """NOTE: /control answers {"code":0,"message":"Success"} to *any* command,
    including nonsense ones — so the reply proves nothing. Confirm by re-reading
    lightStatus instead of trusting it."""
    want_on = bool((request.get_json(silent=True) or {}).get("on"))
    ff_control("lightControl_cmd", {"status": "open" if want_on else "close"})

    for _ in range(4):
        time.sleep(0.8)
        detail, _err = fetch_detail()
        if detail:
            now_on = str(detail.get("lightStatus", "")).lower() in ("open", "on", "1", "true")
            if now_on == want_on:
                return jsonify({"ok": True, "on": now_on, "confirmed": True})
    detail, _err = fetch_detail()
    now_on = str((detail or {}).get("lightStatus", "")).lower() in ("open", "on", "1", "true")
    printing = str((detail or {}).get("status", "")).lower() == "printing"
    return jsonify({
        "ok": False, "on": now_on, "confirmed": False,
        "reason": "The printer accepted the command but did not change lightStatus"
                  + (" — it appears to lock the light on during a print." if printing else "."),
    })


@app.route("/camera/thumb")
def camera_thumb():
    """Current job thumbnail — plain GET on the printer, no auth body."""
    try:
        r = requests.get(f"{BASE}/getThum", timeout=8)
        r.raise_for_status()
    except Exception as exc:
        return Response(f"no thumbnail: {exc}", status=502)
    return Response(r.content,
                    content_type=r.headers.get("Content-Type", "image/bmp"),
                    headers={"Cache-Control": "no-store"})


@app.route("/api/camera", methods=["POST"])
def api_camera():
    """The printer keeps its MJPEG server gated until the stream is enabled."""
    want_on = bool((request.get_json(silent=True) or {}).get("on"))
    ok, info = ff_control("streamCtrl", {"action": "open" if want_on else "close"})
    return jsonify({"ok": ok, "on": want_on, "detail": info})


@app.route("/api/history")
def api_history():
    hist = load_history()
    jobs = hist.get("jobs", [])
    return jsonify({
        "count": len(jobs),
        "total_duration_s": sum(j.get("duration_s") or 0 for j in jobs),
        "jobs": list(reversed(jobs[-50:])),
    })


@app.route("/camera/stream")
def camera_stream():
    """Proxy the printer MJPEG so the dashboard works off-LAN via Caddy."""
    # The printer serves exactly one MJPEG client per "streamCtrl open": once a
    # client disconnects the stream server stops again. So always re-enable
    # before connecting, and give the encoder a couple of seconds to come up.
    upstream = None
    last_exc = None
    for attempt in range(4):
        ff_control("streamCtrl", {"action": "open"})
        time.sleep(2.0 if attempt == 0 else 3.0)
        try:
            upstream = requests.get(CAM_URL, stream=True, timeout=10)
            break
        except Exception as exc:
            last_exc = exc
    if upstream is None:
        return Response(f"camera unavailable: {last_exc}", status=502)

    ctype = upstream.headers.get("Content-Type", "multipart/x-mixed-replace; boundary=boundarydonotcross")

    def gen():
        try:
            for chunk in upstream.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        except Exception:
            pass
        finally:
            upstream.close()

    return Response(gen(), content_type=ctype,
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


threading.Thread(target=poller, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
