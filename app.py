"""
app.py — Production Flask UI for the trading bot.

Run:
    python app.py
    open http://localhost:5000
"""

import os
import pathlib
import threading
import time
from flask import Flask, jsonify, render_template, request

import yaml
import logger

# ── Absolute paths (safe regardless of where you launch from) ─────────────────
BASE_DIR = pathlib.Path(__file__).parent.resolve()
CONFIG_PATH = BASE_DIR / "config.yaml"
LOG_FILE = str(BASE_DIR / "bot.log")
EVENT_LOG = str(BASE_DIR / "bot_events.jsonl")

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))

# ── Bot state ─────────────────────────────────────────────────────────────────

_bot_thread: threading.Thread | None = None
_stop_event = threading.Event()
_bot_status = {
    "running": False,
    "started_at": None,
    "config": {},
    "error": None,
}


def _run_bot(cfg: dict) -> None:
    """Target function for the bot background thread."""
    global _bot_status
    try:
        from exchange_client import build_client
        from execution import ExecutionEngine
        from risk_manager import RiskManager
        from strategy import Strategy

        log_cfg = cfg.get("logging", {})
        logger.setup(
            level=log_cfg.get("level", "INFO"),
            log_file=LOG_FILE,
        )

        client = build_client(cfg)
        risk = RiskManager(cfg, client)
        engine = ExecutionEngine(cfg, client)
        strat = Strategy(cfg, client, risk, engine)
        strat.run_forever(stop_event=_stop_event)

    except Exception as exc:
        _bot_status["error"] = str(exc)
        logger.error("[App] Bot thread crashed", exc)
    finally:
        _bot_status["running"] = False
        _bot_status["started_at"] = None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/status")
def status():
    return jsonify(_bot_status)


@app.route("/start", methods=["POST"])
def start():
    global _bot_thread, _stop_event, _bot_status

    if _bot_status["running"]:
        return jsonify({"ok": False, "error": "Bot is already running."}), 400

    if not CONFIG_PATH.exists():
        return jsonify({"ok": False, "error": f"config.yaml not found at {CONFIG_PATH}"}), 500

    data = request.json or {}

    api_key = data.get("api_key", "").strip()
    api_secret = data.get("api_secret", "").strip()
    token = data.get("token", "BTC/USDT").strip()
    delay = int(data.get("delay", 30))
    amount_type = data.get("amount_type", "fixed")   # fixed | percent
    amount_value = float(data.get("amount_value", 100))
    mode = data.get("mode", "paper")

    if mode == "live" and (not api_key or not api_secret):
        return jsonify({"ok": False, "error": "API key and secret are required for live mode."}), 400

    if api_key:
        os.environ["EXCHANGE_API_KEY"] = api_key
    if api_secret:
        os.environ["EXCHANGE_API_SECRET"] = api_secret

    with open(CONFIG_PATH) as fh:
        cfg = yaml.safe_load(fh)

    cfg["mode"] = mode
    cfg["exchange"]["symbol"] = token
    cfg["strategy"]["sell_delay_seconds"] = delay
    cfg["logging"]["log_file"] = LOG_FILE

    cfg["_ui"] = {
        "amount_type": amount_type,
        "amount_value": amount_value,
    }

    if amount_type == "fixed":
        cfg["risk"]["max_position_size_usd"] = amount_value
    else:
        cfg["_ui"]["percent"] = amount_value / 100.0

    cfg["logging"]["level"] = "INFO"

    _stop_event.clear()
    _bot_status.update({
        "running": True,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "error": None,
        "config": {
            "token": token,
            "delay": delay,
            "amount_type": amount_type,
            "amount_value": amount_value,
            "mode": mode,
        },
    })

    _bot_thread = threading.Thread(target=_run_bot, args=(cfg,), daemon=True)
    _bot_thread.start()

    return jsonify({"ok": True})


@app.route("/events")
def events():
    """Return last N lines from the JSONL event log."""
    import json as _json
    n = int(request.args.get("n", 40))
    rows = []
    try:
        with open(EVENT_LOG) as fh:
            lines = fh.readlines()
        for raw in lines[-n:]:
            try:
                rows.append(_json.loads(raw))
            except Exception:
                pass
        rows.reverse()
    except FileNotFoundError:
        pass
    return jsonify(rows)


@app.route("/stop", methods=["POST"])
def stop():
    global _bot_status
    if not _bot_status["running"]:
        return jsonify({"ok": False, "error": "Bot is not running."}), 400
    _stop_event.set()
    _bot_status["running"] = False
    _bot_status["started_at"] = None
    return jsonify({"ok": True})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Change CWD to the project folder so relative imports always work
    os.chdir(BASE_DIR)

    port = int(os.environ.get("PORT", 5000))
    print(f"Starting bot dashboard at http://localhost:{port}")
    print("Press Ctrl+C to stop.")

    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=4)
    except ImportError:
        # Fallback to Flask dev server if waitress not installed
        app.run(host="0.0.0.0", port=port, debug=False)
