"""
main.py — Entry point for the crypto trading bot.

Usage:
    # Paper trading (safe, no real money)
    python main.py

    # Live trading (requires API keys in environment)
    MODE=live python main.py

    # Override config file
    python main.py --config my_config.yaml

Environment variables:
    MODE                  paper | live  (overrides config.yaml)
    EXCHANGE_API_KEY      required for live mode
    EXCHANGE_API_SECRET   required for live mode
    EXCHANGE_PASSWORD     optional (Kraken / OKX passphrase)
"""

import argparse
import os
import sys
import threading

import yaml

import logger
from exchange_client import build_client
from execution import ExecutionEngine
from risk_manager import RiskManager
from strategy import Strategy


def load_config(path: str) -> dict:
    with open(path) as fh:
        cfg = yaml.safe_load(fh)
    # Allow MODE env var to override config
    env_mode = os.environ.get("MODE")
    if env_mode:
        cfg["mode"] = env_mode
    return cfg


def validate_live_env() -> None:
    missing = [v for v in ("EXCHANGE_API_KEY", "EXCHANGE_API_SECRET")
               if not os.environ.get(v)]
    if missing:
        print(
            f"[ERROR] Live mode requires env vars: {', '.join(missing)}\n"
            "Set them and try again, or run in paper mode (MODE=paper).",
            file=sys.stderr,
        )
        sys.exit(1)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Crypto trading bot.")
    p.add_argument(
        "--config", "-c", default="config.yaml",
        help="Path to config YAML (default: config.yaml)"
    )
    p.add_argument(
        "--once", action="store_true",
        help="Run a single signal poll and trade cycle then exit (useful for testing)."
    )
    return p


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    # ── Load config ───────────────────────────────────────────────────────────
    cfg = load_config(args.config)
    mode = cfg.get("mode", "paper")

    # ── Logging ───────────────────────────────────────────────────────────────
    log_cfg = cfg.get("logging", {})
    logger.setup(
        level=log_cfg.get("level", "INFO"),
        log_file=log_cfg.get("log_file", "bot.log"),
        rotate_bytes=int(log_cfg.get("rotate_bytes", 10_485_760)),
        backup_count=int(log_cfg.get("backup_count", 5)),
    )
    logger.info(f"[Main] Bot starting  mode={mode}  config={args.config}")

    # ── Live-mode guard ───────────────────────────────────────────────────────
    if mode == "live":
        validate_live_env()
        logger.warning(
            "[Main] LIVE MODE ACTIVE — real orders will be placed. "
            "Press Ctrl+C to abort."
        )

    # ── Build components ──────────────────────────────────────────────────────
    try:
        client = build_client(cfg)
        risk = RiskManager(cfg, client)
        engine = ExecutionEngine(cfg, client)
        strat = Strategy(cfg, client, risk, engine)
    except Exception as exc:
        logger.error("[Main] Initialisation failed", exc)
        return 1

    # ── Print startup summary ─────────────────────────────────────────────────
    balance = client.get_balance()
    logger.info(
        f"[Main] Initial balance  quote={balance.quote:.2f}  base={balance.base:.8f}"
    )
    logger.info(
        f"[Main] Risk limits  "
        f"max_pos={cfg['risk']['max_position_size_usd']}  "
        f"max_daily_loss={cfg['risk']['max_daily_loss_usd']}  "
        f"max_trades/hr={cfg['risk']['max_trades_per_hour']}"
    )

    # ── Run ───────────────────────────────────────────────────────────────────
    if args.once:
        # Single-shot mode: useful for CI / smoke tests
        book = client.get_order_book(cfg["exchange"]["symbol"])
        from strategy import get_buy_signal
        signal = get_buy_signal(cfg["exchange"]["symbol"], book)
        if signal:
            success = strat.run_trade_cycle(signal, book)
            logger.info(f"[Main] Single-shot completed  success={success}")
        else:
            logger.info("[Main] No signal on single-shot poll.")
        return 0

    stop = threading.Event()
    try:
        strat.run_forever(stop_event=stop)
    except KeyboardInterrupt:
        stop.set()
        logger.info("[Main] Shutdown requested by user.")

    logger.info(
        f"[Main] Session ended  "
        f"daily_pnl={risk.daily_pnl:.4f}  "
        f"consecutive_losses={risk.consecutive_losses}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
