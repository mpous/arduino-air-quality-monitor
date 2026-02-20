#!/usr/bin/env python3
"""
Air Quality Monitor — Flask web application for Arduino UNO Q / App Lab.

Responsibilities
────────────────
1. Receive sensor readings from the MCU over the Arduino Bridge.
2. Keep a rolling ring-buffer for real-time chart display (SSE).
3. Record labelled windows and upload them to Edge Impulse (Ingestion API).
4. Run sensor-fusion inference with a downloaded .eim model and push the
   results back to connected browsers via the same SSE stream.

Access the UI at http://<device-ip>:5001
"""

# ── Mocks MUST come before any edge_impulse_linux import ─────────────────────
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from utils.mock_dependencies import apply_mocks
apply_mocks()

# ── Standard library ──────────────────────────────────────────────────────────
import json
import time
import queue
import logging
import threading
from collections import deque
from datetime import datetime, timezone

# ── Third-party ───────────────────────────────────────────────────────────────
import requests
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

SENSORS = ["co2", "temperature", "humidity", "pressure", "temp_dps310"]
SENSOR_META = {
    "co2":         {"label": "CO₂",         "unit": "ppm",  "color": "#4CAF50", "ymin": 300,  "ymax": 5000},
    "temperature": {"label": "Temperature",  "unit": "°C",   "color": "#FF9800", "ymin": -10,  "ymax": 50},
    "humidity":    {"label": "Humidity",     "unit": "%RH",  "color": "#2196F3", "ymin": 0,    "ymax": 100},
    "pressure":    {"label": "Pressure",     "unit": "hPa",  "color": "#9C27B0", "ymin": 900,  "ymax": 1100},
    "temp_dps310": {"label": "Temp (DPS310)","unit": "°C",   "color": "#FF5722", "ymin": -10,  "ymax": 50},
}

BUFFER_SIZE  = 150          # ≈ 5 minutes at 2 s per sample
MODEL_PATH   = os.path.join(os.path.dirname(__file__), "..", "models", "model.eim")
CONFIG_PATH  = os.path.join(os.path.dirname(__file__), "..", "config.json")

# ─────────────────────────────────────────────────────────────────────────────
# Shared state (thread-safe via locks)
# ─────────────────────────────────────────────────────────────────────────────

data_lock = threading.Lock()
data_buf  = {s: deque(maxlen=BUFFER_SIZE) for s in SENSORS}
ts_buf    = deque(maxlen=BUFFER_SIZE)
latest    = {s: None for s in SENSORS}
latest["timestamp"] = None

rec_lock   = threading.Lock()
recording  = False
rec_buf: list = []
rec_label  = "idle"

infer_lock      = threading.Lock()
inference_result = None

runner       = None
model_info   = None
model_loaded = False

# ── Config (persisted to config.json) ────────────────────────────────────────

_default_cfg = {
    "ei_api_key":    os.environ.get("EI_API_KEY", ""),
    "ei_project_id": os.environ.get("EI_PROJECT_ID", ""),
    "interval_ms":   2000,
    "window_ms":     10000,
    "auto_infer":    False,
}


def _load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                saved = json.load(f)
            cfg = dict(_default_cfg)
            cfg.update(saved)
            return cfg
        except Exception:
            pass
    return dict(_default_cfg)


def _save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        log.warning("Could not save config: %s", e)


cfg = _load_config()

# ─────────────────────────────────────────────────────────────────────────────
# SSE fan-out
# ─────────────────────────────────────────────────────────────────────────────

_sse_lock   = threading.Lock()
_sse_queues: dict[int, queue.Queue] = {}


def _publish(payload: dict) -> None:
    msg = json.dumps(payload)
    with _sse_lock:
        for q in list(_sse_queues.values()):
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Bridge (MCU → MPU sensor data)
# ─────────────────────────────────────────────────────────────────────────────

_bridge = None
try:
    from arduino.bridge import Bridge  # type: ignore[import]
    _bridge = Bridge()
    log.info("Bridge initialised")
except ImportError:
    log.warning("Bridge not available — running in mock/simulation mode")


def _handle_sensor_update(data: str) -> None:
    """Parse comma-separated payload from MCU and store into shared buffers."""
    try:
        parts = [float(x) for x in data.split(",")]
        if len(parts) < 5:
            return
        co2, temp_scd, hum, pres, temp_dps = parts[:5]
        ts = datetime.now(timezone.utc).isoformat()

        reading = {
            "co2":         co2,
            "temperature": temp_scd,
            "humidity":    hum,
            "pressure":    pres,
            "temp_dps310": temp_dps,
            "timestamp":   ts,
        }

        with data_lock:
            for s in SENSORS:
                data_buf[s].append(reading[s])
            ts_buf.append(ts)
            latest.update(reading)

        with rec_lock:
            if recording:
                rec_buf.append(reading)

        _publish({
            "type":    "sensor",
            "sensors": {s: reading[s] for s in SENSORS},
            "ts":      ts,
            "rec":     recording,
            "rec_n":   len(rec_buf),
        })

        if cfg.get("auto_infer") and model_loaded:
            _run_inference_bg()

    except (ValueError, IndexError) as exc:
        log.debug("Bad sensor payload '%s': %s", data, exc)


if _bridge:
    @_bridge.on_notify("sensor_update")  # type: ignore[misc]
    def _on_bridge_sensor(data: str) -> None:
        _handle_sensor_update(data)


# ─────────────────────────────────────────────────────────────────────────────
# Mock sensor thread (used when Bridge is unavailable)
# ─────────────────────────────────────────────────────────────────────────────

def _mock_thread() -> None:
    import math
    import random
    t = 0.0
    while True:
        co2  = 420 + 180 * math.sin(t / 30)  + random.gauss(0, 8)
        temp = 22  +   3 * math.sin(t / 60)  + random.gauss(0, 0.3)
        hum  = 52  +  10 * math.sin(t / 45)  + random.gauss(0, 0.8)
        pres = 1013 +  4 * math.sin(t / 120) + random.gauss(0, 0.3)
        tdps = temp + random.gauss(0, 0.15)
        _handle_sensor_update(f"{co2:.1f},{temp:.2f},{hum:.2f},{pres:.2f},{tdps:.2f}")
        t += 2
        time.sleep(2)


# ─────────────────────────────────────────────────────────────────────────────
# Edge Impulse — upload
# ─────────────────────────────────────────────────────────────────────────────

def upload_to_ei(samples: list, label: str) -> dict:
    """
    Upload a list of sensor dicts to Edge Impulse via the Ingestion API.
    Uses the unsigned JSON envelope format for sensor-fusion time-series.
    """
    api_key = cfg.get("ei_api_key", "")
    if not api_key:
        return {"success": False, "error": "EI API key not configured"}
    if not samples:
        return {"success": False, "error": "No samples to upload"}

    interval_ms = int(cfg.get("interval_ms", 2000))
    values = [
        [s["co2"], s["temperature"], s["humidity"], s["pressure"], s["temp_dps310"]]
        for s in samples
    ]

    envelope = {
        "protected": {"ver": "v1", "alg": "none", "iat": int(time.time())},
        "signature": "0" * 64,
        "payload": {
            "device_name":  "arduino-uno-q",
            "device_type":  "ARDUINO_UNO_Q",
            "interval_ms":  interval_ms,
            "sensors": [
                {"name": "co2",         "units": "ppm"},
                {"name": "temperature", "units": "C"},
                {"name": "humidity",    "units": "%"},
                {"name": "pressure",    "units": "hPa"},
                {"name": "temp_dps310", "units": "C"},
            ],
            "values": values,
        },
    }

    filename = f"{label}.{int(time.time())}.json"
    try:
        resp = requests.post(
            "https://ingestion.edgeimpulse.com/api/training/data",
            headers={
                "x-api-key":    api_key,
                "x-label":      label,
                "Content-Type": "application/json",
                "X-File-Name":  filename,
            },
            json=envelope,
            timeout=30,
        )
        if resp.status_code == 200:
            log.info("Uploaded %d samples as '%s' → %s", len(samples), label, filename)
            return {"success": True, "filename": filename, "count": len(samples)}
        return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# Edge Impulse — inference
# ─────────────────────────────────────────────────────────────────────────────

def load_model(path: str) -> bool:
    global runner, model_info, model_loaded
    try:
        from edge_impulse_linux.runner import ImpulseRunner  # type: ignore[import]
        if runner is not None:
            try:
                runner.stop()
            except Exception:
                pass
        runner      = ImpulseRunner(path)
        model_info  = runner.init()
        model_loaded = True
        name = model_info.get("project", {}).get("name", path)
        log.info("Model loaded: %s  features=%d  labels=%s",
                 name,
                 model_info["model_parameters"]["input_features_count"],
                 model_info["model_parameters"].get("labels", []))
        return True
    except Exception as exc:
        log.error("Failed to load model '%s': %s", path, exc)
        runner = model_info = None
        model_loaded = False
        return False


def _run_inference_bg() -> None:
    """Fire-and-forget inference in a daemon thread."""
    threading.Thread(target=_do_inference, daemon=True).start()


def _do_inference() -> dict:
    global inference_result
    if not model_loaded or runner is None or model_info is None:
        return {"success": False, "error": "No model loaded"}

    try:
        n_features = model_info["model_parameters"]["input_features_count"]
        n_sensors  = len(SENSORS)
        n_samples  = n_features // n_sensors

        with data_lock:
            available = len(ts_buf)
            if available < n_samples:
                return {"success": False,
                        "error": f"Need {n_samples} samples, have {available}"}
            features = []
            for i in range(-n_samples, 0):
                for s in SENSORS:
                    features.append(float(data_buf[s][i]))

        raw = runner.classify(features)
        classification = raw.get("result", {}).get("classification", {})
        anomaly        = raw.get("result", {}).get("anomaly", None)

        result = {
            "classification": classification,
            "anomaly":        anomaly,
            "ts":             datetime.now(timezone.utc).isoformat(),
        }
        with infer_lock:
            inference_result = result

        _publish({"type": "inference", "result": result})
        log.info("Inference: %s  anomaly=%.3f", classification, anomaly or 0)
        return {"success": True, "result": result}

    except Exception as exc:
        log.error("Inference error: %s", exc)
        return {"success": False, "error": str(exc)}


# Auto-load model if the file already exists
if os.path.isfile(MODEL_PATH):
    load_model(MODEL_PATH)

# ─────────────────────────────────────────────────────────────────────────────
# Flask application
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__, template_folder="templates")


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return render_template(
        "index.html",
        sensors=SENSORS,
        sensor_meta=SENSOR_META,
    )


# ── Server-Sent Events ────────────────────────────────────────────────────────

@app.get("/events")
def events():
    def _generate():
        cid = id(threading.current_thread())
        q: queue.Queue = queue.Queue(maxsize=200)
        with _sse_lock:
            _sse_queues[cid] = q

        # Send current state immediately so the page loads with data
        with data_lock:
            init_payload = {
                "type":    "sensor",
                "sensors": {s: latest.get(s) for s in SENSORS},
                "ts":      latest.get("timestamp"),
                "rec":     recording,
                "rec_n":   len(rec_buf),
            }
        with infer_lock:
            init_payload["inference"] = inference_result

        yield f"data: {json.dumps(init_payload)}\n\n"

        try:
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    yield ":\n\n"  # keep-alive heartbeat
        finally:
            with _sse_lock:
                _sse_queues.pop(cid, None)

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get("/api/history")
def api_history():
    """Return full ring-buffer contents for initial chart population."""
    with data_lock:
        return jsonify({
            "timestamps": list(ts_buf),
            "sensors":    {s: list(data_buf[s]) for s in SENSORS},
        })


@app.get("/api/status")
def api_status():
    with infer_lock:
        infer = inference_result
    return jsonify({
        "bridge":      _bridge is not None,
        "model":       model_loaded,
        "model_path":  MODEL_PATH if model_loaded else None,
        "recording":   recording,
        "rec_count":   len(rec_buf),
        "label":       rec_label,
        "ei_ready":    bool(cfg.get("ei_api_key")),
        "buf_count":   len(ts_buf),
        "auto_infer":  cfg.get("auto_infer", False),
        "inference":   infer,
    })


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        # Never expose the raw API key to the browser
        safe = {k: v for k, v in cfg.items() if k != "ei_api_key"}
        safe["ei_api_key_set"] = bool(cfg.get("ei_api_key"))
        return jsonify(safe)

    data = request.get_json(silent=True) or {}
    for key in ("ei_api_key", "ei_project_id", "interval_ms", "window_ms", "auto_infer"):
        if key in data:
            cfg[key] = data[key]
    _save_config(cfg)
    return jsonify({"success": True})


@app.post("/api/record/start")
def api_record_start():
    global recording, rec_buf, rec_label
    body = request.get_json(silent=True) or {}
    label = str(body.get("label", "idle")).strip() or "idle"
    with rec_lock:
        recording = True
        rec_buf   = []
        rec_label = label
    log.info("Recording started — label='%s'", label)
    return jsonify({"success": True, "label": label})


@app.post("/api/record/stop")
def api_record_stop():
    global recording
    with rec_lock:
        recording = False
        count = len(rec_buf)
    log.info("Recording stopped — %d samples", count)
    return jsonify({"success": True, "count": count})


@app.post("/api/upload")
def api_upload():
    with rec_lock:
        if not rec_buf:
            return jsonify({"success": False, "error": "No recorded data"}), 400
        samples = list(rec_buf)
        label   = rec_label
    result = upload_to_ei(samples, label)
    return jsonify(result)


@app.post("/api/infer")
def api_infer():
    result = _do_inference()
    return jsonify(result)


@app.post("/api/model/load")
def api_model_load():
    body = request.get_json(silent=True) or {}
    path = body.get("path", MODEL_PATH)
    ok   = load_model(path)
    return jsonify({"success": ok, "model_loaded": model_loaded})


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if _bridge is None:
        log.info("Starting mock sensor thread")
        threading.Thread(target=_mock_thread, daemon=True).start()

    log.info("Starting Air Quality Monitor on http://0.0.0.0:5001")
    app.run(host="0.0.0.0", port=5001, threaded=True)
