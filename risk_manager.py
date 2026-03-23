"""
risk_manager.py — Pre-trade and post-trade risk checks.

All public methods return (approved: bool, reason: str).
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional, Tuple

import logger
from exchange_client import ExchangeClient, OrderBook


@dataclass
class TradeRecord:
    timestamp: float
    realized_pnl: float


class RiskManager:

    def __init__(self, cfg: dict, client: ExchangeClient) -> None:
        rcfg = cfg.get("risk", {})
        fcfg = cfg.get("fees", {})

        self._client = client
        self._max_position_usd: float = float(rcfg.get("max_position_size_usd", 500.0))
        self._max_daily_loss: float = float(rcfg.get("max_daily_loss_usd", 200.0))
        self._max_trades_per_hour: int = int(rcfg.get("max_trades_per_hour", 10))
        self._cooldown_secs: float = float(
            rcfg.get("consecutive_loss_cooldown_seconds", 300)
        )
        self._loss_threshold: int = int(rcfg.get("consecutive_losses_threshold", 3))
        self._min_balance_usd: float = float(rcfg.get("min_balance_usd", 50.0))
        self._min_depth_usd: float = float(rcfg.get("min_order_book_depth_usd", 10_000.0))
        self._max_slippage_pct: float = float(rcfg.get("max_slippage_pct", 0.30)) / 100.0
        self._max_fee_pct: float = float(rcfg.get("max_fee_pct", 0.20)) / 100.0
        self._taker_fee: float = float(fcfg.get("taker_pct", 0.10)) / 100.0

        # State
        self._daily_pnl: float = 0.0
        self._day_start: float = self._today_start()
        self._recent_trades: Deque[float] = deque()        # timestamps (last hour)
        self._consecutive_losses: int = 0
        self._cooldown_until: float = 0.0
        self._has_open_position: bool = False
        self._trade_history: list = []

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _today_start() -> float:
        import datetime
        now = datetime.datetime.utcnow()
        return datetime.datetime(now.year, now.month, now.day).timestamp()

    def _reset_daily_if_needed(self) -> None:
        if time.time() >= self._day_start + 86_400:
            logger.info("[Risk] New trading day — resetting daily counters.")
            self._daily_pnl = 0.0
            self._day_start = self._today_start()

    def _prune_recent_trades(self) -> None:
        cutoff = time.time() - 3600.0
        while self._recent_trades and self._recent_trades[0] < cutoff:
            self._recent_trades.popleft()

    def update_settings(self, min_balance: float, min_liquidity: float, max_position: float) -> None:
        self._min_balance_usd = min_balance
        self._min_depth_usd = min_liquidity
        self._max_position_usd = max_position
        logger.info(
            f"[Risk] Settings updated  min_balance={min_balance}  "
            f"min_liquidity={min_liquidity}  max_position={max_position}"
        )

    # ── Position state ────────────────────────────────────────────────────────

    def set_position_open(self) -> None:
        self._has_open_position = True

    def set_position_closed(self, realized_pnl: float) -> None:
        self._has_open_position = False
        self._recent_trades.append(time.time())
        self._daily_pnl += realized_pnl
        self._trade_history.append(
            TradeRecord(timestamp=time.time(), realized_pnl=realized_pnl)
        )
        if realized_pnl < 0:
            self._consecutive_losses += 1
            if self._consecutive_losses >= self._loss_threshold:
                self._cooldown_until = time.time() + self._cooldown_secs
                logger.warning(
                    f"[Risk] {self._consecutive_losses} consecutive losses — "
                    f"cooldown for {self._cooldown_secs}s"
                )
        else:
            self._consecutive_losses = 0

    # ── Pre-trade checks ──────────────────────────────────────────────────────

    def check_all(
        self,
        symbol: str,
        order_book: OrderBook,
        quantity_base: float,
        mid_price: float,
        allow_overlapping: bool = False,
    ) -> Tuple[bool, str]:
        """
        Run every risk check.  Returns (True, "ok") or (False, reason).
        """
        self._reset_daily_if_needed()
        checks = [
            self._check_cooldown,
            lambda: self._check_open_position(allow_overlapping),
            lambda: self._check_balance(quantity_base, mid_price),
            lambda: self._check_daily_loss(),
            lambda: self._check_trades_per_hour(),
            lambda: self._check_liquidity(order_book),
            lambda: self._check_slippage(order_book, quantity_base, mid_price),
            lambda: self._check_fees(quantity_base, mid_price),
        ]
        for check in checks:
            ok, reason = check()
            if not ok:
                logger.risk_rejected(reason)
                return False, reason
        return True, "ok"

    def _check_cooldown(self) -> Tuple[bool, str]:
        remaining = self._cooldown_until - time.time()
        if remaining > 0:
            return False, f"cooldown_active ({remaining:.0f}s remaining)"
        return True, "ok"

    def _check_open_position(self, allow_overlapping: bool) -> Tuple[bool, str]:
        if self._has_open_position and not allow_overlapping:
            return False, "open_position_exists"
        return True, "ok"

    def _check_balance(self, qty: float, price: float) -> Tuple[bool, str]:
        balance = self._client.get_balance()
        if balance.quote < self._min_balance_usd:
            return False, f"balance_too_low ({balance.quote:.2f} < {self._min_balance_usd})"
        trade_value = qty * price
        if trade_value > self._max_position_usd:
            return False, (
                f"position_too_large ({trade_value:.2f} > {self._max_position_usd})"
            )
        if balance.quote < trade_value:
            return False, f"insufficient_funds ({balance.quote:.2f} < {trade_value:.2f})"
        return True, "ok"

    def _check_daily_loss(self) -> Tuple[bool, str]:
        if self._daily_pnl <= -self._max_daily_loss:
            return False, (
                f"daily_loss_limit_hit ({self._daily_pnl:.2f} <= -{self._max_daily_loss})"
            )
        return True, "ok"

    def _check_trades_per_hour(self) -> Tuple[bool, str]:
        self._prune_recent_trades()
        if len(self._recent_trades) >= self._max_trades_per_hour:
            return False, (
                f"max_trades_per_hour ({len(self._recent_trades)} >= {self._max_trades_per_hour})"
            )
        return True, "ok"

    def _check_liquidity(self, book: OrderBook) -> Tuple[bool, str]:
        ask_depth = book.ask_depth_usd(5)
        bid_depth = book.bid_depth_usd(5)
        min_depth = self._min_depth_usd
        if ask_depth < min_depth or bid_depth < min_depth:
            return False, (
                f"insufficient_liquidity  ask_depth={ask_depth:.0f}  "
                f"bid_depth={bid_depth:.0f}  threshold={min_depth}"
            )
        return True, "ok"

    def _check_slippage(
        self, book: OrderBook, qty: float, mid_price: float
    ) -> Tuple[bool, str]:
        """Walk the ask side to estimate fill price for a market buy."""
        remaining = qty
        total_cost = 0.0
        for price, size in book.asks:
            take = min(remaining, size)
            total_cost += take * price
            remaining -= take
            if remaining <= 0:
                break
        if remaining > 0:
            return False, "not_enough_asks_to_fill_order"
        avg_fill = total_cost / qty
        slippage = (avg_fill - mid_price) / mid_price
        if slippage > self._max_slippage_pct:
            return False, (
                f"slippage_too_high ({slippage * 100:.4f}% > "
                f"{self._max_slippage_pct * 100:.4f}%)"
            )
        return True, "ok"

    def _check_fees(self, qty: float, price: float) -> Tuple[bool, str]:
        """Estimate round-trip fee cost as a percentage of trade value."""
        trade_value = qty * price
        round_trip_fee = trade_value * self._taker_fee * 2  # buy + sell
        fee_pct = round_trip_fee / trade_value
        if fee_pct > self._max_fee_pct:
            return False, (
                f"fees_too_high ({fee_pct * 100:.4f}% > {self._max_fee_pct * 100:.4f}%)"
            )
        return True, "ok"

    # ── Accessors ─────────────────────────────────────────────────────────────

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    @property
    def has_open_position(self) -> bool:
        return self._has_open_position
