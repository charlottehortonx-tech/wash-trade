"""
logger.py — Structured event logger for the trading bot.

Every significant event is logged as a structured JSON line so it can
be ingested by any log aggregation pipeline (ELK, Datadog, etc.).
Human-readable lines are also written to stdout/file via standard logging.
"""

import json
import logging
import logging.handlers
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional


# ── Module-level logger (configured once via setup()) ─────────────────────────
_log: Optional[logging.Logger] = None
_event_log: Optional[logging.Logger] = None


def setup(level: str = "INFO", log_file: str = "bot.log",
          rotate_bytes: int = 10_485_760, backup_count: int = 5) -> None:
    """Initialise both human-readable and structured event loggers."""
    global _log, _event_log

    numeric_level = getattr(logging, level.upper(), logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%dT%H:%M:%S"

    # Human-readable logger
    _log = logging.getLogger("bot")
    _log.setLevel(numeric_level)
    if not _log.handlers:
        # Console
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
        _log.addHandler(ch)
        # Rotating file
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=rotate_bytes, backupCount=backup_count
        )
        fh.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
        _log.addHandler(fh)

    # Structured JSON event logger (separate file)
    event_file = log_file.replace(".log", "_events.jsonl")
    _event_log = logging.getLogger("bot.events")
    _event_log.setLevel(logging.DEBUG)
    _event_log.propagate = False
    if not _event_log.handlers:
        efh = logging.handlers.RotatingFileHandler(
            event_file, maxBytes=rotate_bytes, backupCount=backup_count
        )
        efh.setFormatter(logging.Formatter("%(message)s"))
        _event_log.addHandler(efh)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit_event(event_type: str, data: Dict[str, Any]) -> None:
    payload = {"ts": _now(), "event": event_type, **data}
    if _event_log:
        _event_log.info(json.dumps(payload))


# ── Convenience accessors ─────────────────────────────────────────────────────

def get() -> logging.Logger:
    if _log is None:
        setup()
    return _log  # type: ignore[return-value]


# ── Typed event helpers ───────────────────────────────────────────────────────

def signal_received(symbol: str, price: float, extra: Optional[Dict] = None) -> None:
    get().info(f"[SIGNAL] BUY signal received  symbol={symbol}  price={price}")
    _emit_event("signal_received", {"symbol": symbol, "price": price, **(extra or {})})


def order_submitted(order_id: str, side: str, symbol: str,
                    qty: float, price: Optional[float]) -> None:
    get().info(
        f"[ORDER] Submitted {side.upper()} {qty} {symbol}  "
        f"order_id={order_id}  price={price}"
    )
    _emit_event("order_submitted",
                {"order_id": order_id, "side": side, "symbol": symbol,
                 "qty": qty, "price": price})


def order_filled(order_id: str, side: str, symbol: str,
                 filled_qty: float, avg_price: float, fee: float) -> None:
    get().info(
        f"[FILL] {side.upper()} filled  order_id={order_id}  "
        f"qty={filled_qty}  avg_price={avg_price}  fee={fee}"
    )
    _emit_event("order_filled",
                {"order_id": order_id, "side": side, "symbol": symbol,
                 "filled_qty": filled_qty, "avg_price": avg_price, "fee": fee})


def order_cancelled(order_id: str, reason: str) -> None:
    get().warning(f"[CANCEL] order_id={order_id}  reason={reason}")
    _emit_event("order_cancelled", {"order_id": order_id, "reason": reason})


def sell_timer_started(delay_seconds: int, filled_qty: float) -> None:
    get().info(f"[TIMER] Sell timer started  delay={delay_seconds}s  qty={filled_qty}")
    _emit_event("sell_timer_started",
                {"delay_seconds": delay_seconds, "filled_qty": filled_qty})


def trade_completed(symbol: str, buy_price: float, sell_price: float,
                    qty: float, buy_fee: float, sell_fee: float,
                    realized_pnl: float) -> None:
    get().info(
        f"[PNL] Trade complete  symbol={symbol}  qty={qty}  "
        f"buy={buy_price}  sell={sell_price}  "
        f"fees={buy_fee + sell_fee:.6f}  pnl={realized_pnl:.6f}"
    )
    _emit_event("trade_completed",
                {"symbol": symbol, "qty": qty, "buy_price": buy_price,
                 "sell_price": sell_price, "buy_fee": buy_fee,
                 "sell_fee": sell_fee, "realized_pnl": realized_pnl})


def risk_rejected(reason: str, details: Optional[Dict] = None) -> None:
    get().warning(f"[RISK] Trade rejected  reason={reason}  {details or {}}")
    _emit_event("risk_rejected", {"reason": reason, **(details or {})})


def error(msg: str, exc: Optional[Exception] = None) -> None:
    get().error(f"[ERROR] {msg}" + (f"  exc={exc}" if exc else ""))
    _emit_event("error", {"msg": msg, "exc": str(exc) if exc else None})


def info(msg: str) -> None:
    get().info(msg)


def warning(msg: str) -> None:
    get().warning(msg)


def debug(msg: str) -> None:
    get().debug(msg)
