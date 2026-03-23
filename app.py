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
from flask import Flask, jsonify, render_template, request, Response

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
_strategy_ref = None  # live reference to running Strategy instance


def _run_bot(cfg: dict) -> None:
    """Target function for the bot background thread."""
    global _bot_status, _strategy_ref
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
        _strategy_ref = strat
        strat.run_forever(stop_event=_stop_event)

    except Exception as exc:
        _bot_status["error"] = str(exc)
        logger.error("[App] Bot thread crashed", exc)
    finally:
        _strategy_ref = None
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
    delay = int(data.get("delay", 45))
    profit_target = float(data.get("profit_target", 0.5))
    limit_sell_offset = float(data.get("limit_sell_offset", 0.1))
    amount_type = data.get("amount_type", "fixed")   # fixed | percent
    amount_value = float(data.get("amount_value", 100))
    min_balance = float(data.get("min_balance", 50))
    mode = data.get("mode", "live")

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
    cfg["strategy"]["profit_target_pct"] = profit_target
    cfg["strategy"]["limit_sell_offset_pct"] = limit_sell_offset
    cfg["logging"]["log_file"] = LOG_FILE

    cfg["_ui"] = {
        "amount_type": amount_type,
        "amount_value": amount_value,
    }

    if amount_type == "fixed":
        cfg["risk"]["max_position_size_usd"] = amount_value
    else:
        cfg["_ui"]["percent"] = amount_value / 100.0

    cfg["risk"]["min_balance_usd"] = min_balance
    cfg["logging"]["level"] = "INFO"

    _stop_event.clear()
    _bot_status.update({
        "running": True,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "error": None,
        "config": {
            "token": token,
            "delay": delay,
            "profit_target": profit_target,
            "limit_sell_offset": limit_sell_offset,
            "min_balance": min_balance,
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


@app.route("/update_config", methods=["POST"])
def update_config():
    """Update strategy settings on the running bot without restarting."""
    global _bot_status
    data = request.json or {}

    delay = float(data.get("delay", 45))
    profit_target = float(data.get("profit_target", 0.5))
    limit_sell_offset = float(data.get("limit_sell_offset", 0.1))
    amount_type = data.get("amount_type", "fixed")
    amount_value = float(data.get("amount_value", 100))

    if _strategy_ref is not None:
        _strategy_ref.update_settings(
            delay=delay,
            profit_target=profit_target,
            limit_sell_offset=limit_sell_offset,
            amount_type=amount_type,
            amount_value=amount_value,
        )

    _bot_status["config"].update({
        "delay": delay,
        "profit_target": profit_target,
        "limit_sell_offset": limit_sell_offset,
        "amount_type": amount_type,
        "amount_value": amount_value,
    })

    return jsonify({"ok": True})



@app.route("/export/csv")
def export_csv():
    """Download all events as a CSV file."""
    import json as _json
    import csv
    import io

    # Collect all known fields across event types for stable column order
    FIELDS = [
        "ts", "event", "symbol", "side", "order_id", "qty", "price",
        "filled_qty", "avg_price", "fee", "buy_price", "sell_price",
        "buy_fee", "sell_fee", "realized_pnl", "delay_seconds",
        "reason", "msg", "exc",
    ]

    rows = []
    try:
        with open(EVENT_LOG) as fh:
            for line in fh:
                try:
                    rows.append(_json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=bot_events.csv"},
    )


@app.route("/export/errors")
def export_errors():
    """Download only error and risk_rejected events as CSV."""
    import json as _json
    import csv
    import io

    ERROR_EVENTS = {"error", "risk_rejected", "order_cancelled"}
    FIELDS = ["ts", "event", "reason", "msg", "exc", "order_id"]

    rows = []
    try:
        with open(EVENT_LOG) as fh:
            for line in fh:
                try:
                    ev = _json.loads(line)
                    if ev.get("event") in ERROR_EVENTS:
                        rows.append(ev)
                except Exception:
                    pass
    except FileNotFoundError:
        pass

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=bot_errors.csv"},
    )


@app.route("/export/log")
def export_log():
    """Download the raw bot.log file."""
    try:
        with open(LOG_FILE) as fh:
            content = fh.read()
    except FileNotFoundError:
        content = "(log file not found)"

    return Response(
        content,
        mimetype="text/plain",
        headers={"Content-Disposition": "attachment; filename=bot.log"},
    )

@app.route("/force_buy", methods=["POST"])
def force_buy():
    """Trigger an immediate market buy, bypassing the signal check."""
    if not _bot_status["running"]:
        return jsonify({"ok": False, "error": "Bot is not running."}), 400

    def _do_force_buy():
        try:
            from exchange_client import build_client
            from execution import ExecutionEngine
            from risk_manager import RiskManager
            from strategy import BuySignal, Strategy

            with open(CONFIG_PATH) as fh:
                cfg = yaml.safe_load(fh)

            running_cfg = _bot_status.get("config", {})
            cfg["mode"] = running_cfg.get("mode", cfg.get("mode", "paper"))
            cfg["exchange"]["symbol"] = running_cfg.get("token", cfg["exchange"]["symbol"])
            cfg["strategy"]["sell_delay_seconds"] = running_cfg.get("delay", 45)
            cfg["strategy"]["profit_target_pct"] = running_cfg.get("profit_target", 0.5)
            cfg["strategy"]["limit_sell_offset_pct"] = running_cfg.get("limit_sell_offset", 0.1)
            amount_type = running_cfg.get("amount_type", "fixed")
            amount_value = float(running_cfg.get("amount_value", 100))
            cfg["_ui"] = {
                "amount_type": amount_type,
                "amount_value": amount_value,
                "percent": amount_value / 100.0,
            }
            if amount_type == "fixed":
                cfg["risk"]["max_position_size_usd"] = amount_value

            client = build_client(cfg)
            risk = RiskManager(cfg, client)
            engine = ExecutionEngine(cfg, client)
            symbol = cfg["exchange"]["symbol"]
            strat = Strategy(cfg, client, risk, engine)
            book = client.get_order_book(symbol)
            qty = strat._calc_qty(book.mid_price)
            signal = BuySignal(symbol=symbol, suggested_quantity_base=qty, source="force_buy")
            strat.run_trade_cycle(signal, book)
        except Exception as exc:
            logger.error("[App] Force buy failed", exc)

    threading.Thread(target=_do_force_buy, daemon=True).start()
    return jsonify({"ok": True})


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
