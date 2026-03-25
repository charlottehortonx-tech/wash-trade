"""
execution.py — Order placement, fill polling, retry logic, and the 30-second sell timer.

Flow:
    1. place_buy()        → submit market/limit buy, get Order
    2. wait_for_fill()    → poll until fully filled or timeout/cancel
    3. (caller waits 30s)
    4. place_sell()       → submit sell for exact filled qty
    5. wait_for_fill()    → poll until fully flat (handles partial fills)
"""

import time
from dataclasses import dataclass
from typing import Optional

import logger
from exchange_client import ExchangeClient, Order


# ── Position snapshot ─────────────────────────────────────────────────────────

@dataclass
class Position:
    symbol: str
    filled_qty: float          # total base quantity bought
    avg_buy_price: float       # average fill price
    buy_fee: float
    buy_order_id: str
    fill_timestamp: float      # epoch seconds of confirmed fill


# ── Execution engine ──────────────────────────────────────────────────────────

class ExecutionEngine:

    def __init__(self, cfg: dict, client: ExchangeClient) -> None:
        ecfg = cfg.get("execution", {})
        ex_cfg = cfg.get("exchange", {})

        self._client = client
        self._symbol: str = ex_cfg.get("symbol", "BTC/USDT")
        self._order_type: str = ecfg.get("order_type", "market")
        self._limit_slippage: float = float(
            ecfg.get("limit_order_slippage_pct", 0.05)
        ) / 100.0
        self._fill_timeout: float = float(ecfg.get("fill_timeout_seconds", 60))
        self._sell_retries: int = int(ecfg.get("sell_retry_attempts", 5))
        self._sell_retry_delay: float = float(ecfg.get("sell_retry_delay_seconds", 3))
        self._stale_timeout: float = float(ecfg.get("stale_order_timeout_seconds", 30))
        self._min_sell_notional: float = float(ecfg.get("min_sell_notional_usd", 1.5))
        self._cut_loss_timeout: float = float(ecfg.get("cut_loss_timeout_seconds", 120))
        self._sell_delay: float = float(
            cfg.get("strategy", {}).get("sell_delay_seconds", 30)
        )
        scfg = cfg.get("strategy", {})
        self._profit_target: float = float(scfg.get("profit_target_pct", 0.5)) / 100.0
        self._limit_sell_offset: float = float(scfg.get("limit_sell_offset_pct", 0.1)) / 100.0

    def update_settings(
        self,
        sell_delay: float,
        profit_target_pct: float,
        limit_sell_offset_pct: float,
    ) -> None:
        self._sell_delay = sell_delay
        self._profit_target = profit_target_pct / 100.0
        self._limit_sell_offset = limit_sell_offset_pct / 100.0
        logger.info(
            f"[Exec] Settings updated  sell_delay={sell_delay}s  "
            f"profit_target={profit_target_pct}%  limit_sell_offset={limit_sell_offset_pct}%"
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _poll_until_filled(
        self, order: Order, timeout: float, poll_interval: float = 1.0
    ) -> Order:
        """
        Poll exchange until order is filled, cancelled, or timeout expires.
        Returns the latest Order state.
        """
        # Short-circuit: exchange may return a terminal status from place_order itself.
        if order.status in ("filled", "cancelled"):
            return order

        deadline = time.time() + timeout
        while time.time() < deadline:
            order = self._client.get_order(order.order_id, order.symbol)
            if order.status == "filled":
                return order
            if order.status == "cancelled":
                logger.warning(
                    f"[Exec] Order {order.order_id} was cancelled externally."
                )
                return order
            time.sleep(poll_interval)

        # Timeout — cancel stale order
        logger.warning(
            f"[Exec] Order {order.order_id} timed out after {timeout}s — cancelling."
        )
        self._client.cancel_order(order.order_id, order.symbol)
        logger.order_cancelled(order.order_id, "fill_timeout")
        # Return last known state
        return self._client.get_order(order.order_id, order.symbol)

    def _compute_buy_price(self, book_mid: float, side: str = "buy") -> Optional[float]:
        """For limit orders, offset mid-price by slippage allowance."""
        if self._order_type != "limit":
            return None
        if side == "buy":
            return round(book_mid * (1 + self._limit_slippage), 8)
        else:
            return round(book_mid * (1 - self._limit_slippage), 8)

    # ── Public API ────────────────────────────────────────────────────────────

    def place_buy(self, quantity: float, mid_price: float) -> Optional[Order]:
        """Submit a BUY order. Returns Order or None if placement fails."""
        price = self._compute_buy_price(mid_price, "buy")
        try:
            order = self._client.place_order(
                symbol=self._symbol,
                side="buy",
                order_type=self._order_type,
                quantity=quantity,
                price=price,
            )
            logger.order_submitted(order.order_id, "buy", self._symbol, quantity, price)
            return order
        except Exception as exc:
            logger.error("Failed to place BUY order", exc)
            return None

    def wait_for_buy_fill(
        self, order: Order, fallback_price: float = 0.0
    ) -> Optional[Position]:
        """
        Wait for the buy order to fill fully.
        Returns a Position on success, None on timeout/cancel.

        fallback_price: used as avg_buy_price when the exchange returns 0.0
        (BinanceTH market orders never populate cummulativeQuoteQty via get_order).
        """
        order = self._poll_until_filled(order, timeout=self._fill_timeout)
        if order.status != "filled" and order.filled_qty == 0:
            logger.error(
                f"Buy order {order.order_id} not filled  status={order.status}"
            )
            return None

        # BinanceTH never returns avg fill price via get_order for market orders.
        # Use the order book mid at signal time as a best-effort fill price.
        avg_price = order.avg_fill_price
        if avg_price == 0.0 and fallback_price > 0.0:
            avg_price = fallback_price
            logger.warning(
                f"[Exec] avg_fill_price=0.0 from exchange — using fallback "
                f"price {fallback_price:.4f} for position tracking"
            )

        # Partial fill: treat filled portion as the position
        pos = Position(
            symbol=self._symbol,
            filled_qty=order.filled_qty,
            avg_buy_price=avg_price,
            buy_fee=order.fee,
            buy_order_id=order.order_id,
            fill_timestamp=time.time(),
        )
        logger.order_filled(
            order.order_id, "buy", self._symbol,
            order.filled_qty, order.avg_fill_price, order.fee,
        )
        return pos

    def wait_sell_delay(self, position: Position) -> None:
        """Block for exactly sell_delay_seconds after the fill timestamp."""
        elapsed = time.time() - position.fill_timestamp
        remaining = self._sell_delay - elapsed
        if remaining > 0:
            logger.sell_timer_started(int(remaining), position.filled_qty)
            time.sleep(remaining)
        else:
            logger.info("[Exec] Sell delay already elapsed; proceeding immediately.")

    def _force_market_sell(self, qty: float, reason: str) -> float:
        """
        Last-resort market sell. Tries up to sell_retry_attempts times.
        Returns fee collected (0.0 if all attempts fail — position may remain open).
        """
        logger.warning(
            f"[Exec] CUT-LOSS triggered ({reason}) — forcing market sell "
            f"qty={qty:.8f}"
        )
        for attempt in range(1, self._sell_retries + 1):
            try:
                order = self._client.place_order(
                    symbol=self._symbol,
                    side="sell",
                    order_type="market",
                    quantity=qty,
                    price=None,
                )
                logger.order_submitted(order.order_id, "sell", self._symbol, qty, None)
                order = self._poll_until_filled(order, timeout=self._fill_timeout)
                if order.filled_qty > 0:
                    logger.order_filled(
                        order.order_id, "sell", self._symbol,
                        order.filled_qty, order.avg_fill_price, order.fee,
                    )
                    logger.info("[Exec] Cut-loss market sell filled — position closed.")
                    return order.fee
            except Exception as exc:
                logger.error(f"[Exec] Cut-loss market sell failed (attempt {attempt})", exc)
            if attempt < self._sell_retries:
                time.sleep(self._sell_retry_delay)
        logger.error(
            "[Exec] Cut-loss market sell exhausted all attempts — "
            "position may still be open. Resolve manually."
        )
        return 0.0

    def place_sell(
        self, position: Position, current_mid: float
    ) -> Optional[float]:
        """
        Place a SELL order for the exact filled quantity with retry logic.

        Sell type is chosen dynamically based on price vs entry:
          - current_mid >= entry * (1 + profit_target_pct)  →  market sell
          - otherwise                                        →  limit sell at entry * (1 + limit_sell_offset_pct)

        Handles partial fills by re-selling the remaining amount.
        Returns total sell fee on success, None on failure.
        """
        entry = position.avg_buy_price
        if entry <= 0:
            logger.error(
                f"[Exec] avg_buy_price is {entry} — cannot compute profit target. "
                "Defaulting to limit sell to avoid accidental loss."
            )
            entry = current_mid  # sell at-market equivalent via limit at current price
        gain_pct = (current_mid / entry - 1) * 100

        if current_mid >= entry * (1 + self._profit_target):
            sell_type = "market"
            sell_price: Optional[float] = None
            logger.info(
                f"[Exec] Price {current_mid:.4f} (+{gain_pct:.2f}%) ≥ "
                f"+{self._profit_target * 100:.2f}% target — market sell"
            )
        else:
            sell_type = "limit"
            sell_price = round(entry * (1 + self._limit_sell_offset), 8)
            logger.info(
                f"[Exec] Price {current_mid:.4f} (+{gain_pct:.2f}%) below target — "
                f"limit sell at {sell_price:.4f} (+{self._limit_sell_offset * 100:.2f}%)"
            )

        remaining_qty = position.filled_qty
        total_fee = 0.0
        cut_loss_deadline = time.time() + self._cut_loss_timeout

        for attempt in range(1, self._sell_retries + 1):
            # Cut-loss: if we've spent too long trying, force a market sell now
            if time.time() >= cut_loss_deadline:
                total_fee += self._force_market_sell(
                    remaining_qty, f"timeout after {self._cut_loss_timeout:.0f}s"
                )
                return total_fee

            # Skip sell if remaining notional is below exchange minimum (dust)
            notional = remaining_qty * current_mid
            if notional < self._min_sell_notional:
                logger.warning(
                    f"[Exec] Remaining qty {remaining_qty:.8f} notional ~${notional:.4f} "
                    f"< min ${self._min_sell_notional} — treating as dust, position closed."
                )
                return total_fee

            try:
                order = self._client.place_order(
                    symbol=self._symbol,
                    side="sell",
                    order_type=sell_type,
                    quantity=remaining_qty,
                    price=sell_price,
                )
                logger.order_submitted(
                    order.order_id, "sell", self._symbol, remaining_qty, sell_price
                )
            except Exception as exc:
                logger.error(f"SELL placement failed (attempt {attempt})", exc)
                if attempt < self._sell_retries:
                    time.sleep(self._sell_retry_delay)
                continue

            # Respect the cut-loss deadline inside the fill poll too
            remaining_timeout = max(1.0, cut_loss_deadline - time.time())
            order = self._poll_until_filled(
                order, timeout=min(self._fill_timeout, remaining_timeout)
            )

            if order.filled_qty > 0:
                total_fee += order.fee
                remaining_qty = round(remaining_qty - order.filled_qty, 10)
                logger.order_filled(
                    order.order_id, "sell", self._symbol,
                    order.filled_qty, order.avg_fill_price, order.fee,
                )

            if remaining_qty <= 0:
                logger.info("[Exec] Fully flat — position closed.")
                return total_fee

            logger.warning(
                f"[Exec] Partial sell  remaining={remaining_qty:.8f}  "
                f"attempt={attempt}/{self._sell_retries}"
            )
            if attempt < self._sell_retries:
                time.sleep(self._sell_retry_delay)

        # Retry loop exhausted — cut-loss market sell as last resort
        total_fee += self._force_market_sell(remaining_qty, "retries exhausted")
        return total_fee
