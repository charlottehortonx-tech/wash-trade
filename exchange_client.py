"""
exchange_client.py — Unified exchange interface.

Two implementations:
  PaperExchangeClient  — in-process simulator, no network calls.
  LiveExchangeClient   — thin wrapper around ccxt (real exchange).

Both expose the same public API so execution.py never needs to branch.
"""

import os
import random
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import logger


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class OrderBook:
    symbol: str
    bids: List[Tuple[float, float]]  # [(price, qty), ...]  best bid first
    asks: List[Tuple[float, float]]  # [(price, qty), ...]  best ask first
    timestamp: float = field(default_factory=time.time)

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else float("inf")

    @property
    def mid_price(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    def bid_depth_usd(self, levels: int = 5) -> float:
        return sum(p * q for p, q in self.bids[:levels])

    def ask_depth_usd(self, levels: int = 5) -> float:
        return sum(p * q for p, q in self.asks[:levels])


@dataclass
class Order:
    order_id: str
    symbol: str
    side: str           # "buy" | "sell"
    order_type: str     # "market" | "limit"
    quantity: float
    price: Optional[float]          # None for market orders
    status: str = "open"            # open | filled | partially_filled | cancelled
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    fee: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class Balance:
    base: float    # e.g. BTC
    quote: float   # e.g. USDT


# ── Abstract interface ────────────────────────────────────────────────────────

class ExchangeClient(ABC):

    @abstractmethod
    def get_order_book(self, symbol: str, depth: int = 20) -> OrderBook:
        ...

    @abstractmethod
    def get_balance(self) -> Balance:
        ...

    @abstractmethod
    def place_order(self, symbol: str, side: str, order_type: str,
                    quantity: float, price: Optional[float] = None) -> Order:
        ...

    @abstractmethod
    def get_order(self, order_id: str, symbol: str) -> Order:
        ...

    @abstractmethod
    def cancel_order(self, order_id: str, symbol: str) -> bool:
        ...

    @abstractmethod
    def get_fee_rate(self, order_type: str = "taker") -> float:
        """Return fee rate as a decimal (e.g. 0.001 = 0.1%)."""
        ...


# ── Paper trading simulator ────────────────────────────────────────────────────

class PaperExchangeClient(ExchangeClient):
    """
    Fully in-process simulator. Fills market orders immediately with a
    configurable random-walk price. Supports partial fills optionally.
    """

    def __init__(self, cfg: dict) -> None:
        pcfg = cfg.get("paper", {})
        self._balance = Balance(
            base=float(pcfg.get("initial_balance_btc", 0.0)),
            quote=float(pcfg.get("initial_balance_usd", 10_000.0)),
        )
        self._fee_pct = float(pcfg.get("simulated_taker_fee_pct", 0.10)) / 100.0
        self._fill_probability = float(pcfg.get("fill_probability", 1.0))
        self._price_source = pcfg.get("simulated_price_source", "random_walk")
        self._fixed_price = float(pcfg.get("fixed_price", 30_000.0))
        self._volatility = float(pcfg.get("random_walk_volatility_pct", 0.05)) / 100.0
        self._current_price = self._fixed_price
        self._orders: Dict[str, Order] = {}
        self._symbol = cfg.get("exchange", {}).get("symbol", "BTC/USDT")
        logger.info("[Paper] Simulator initialised  "
                    f"balance_usd={self._balance.quote}  price={self._current_price}")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _tick_price(self) -> float:
        """Apply a random walk step and return new price."""
        if self._price_source == "fixed":
            return self._fixed_price
        drift = random.gauss(0, self._volatility)
        self._current_price = max(1.0, self._current_price * (1 + drift))
        return self._current_price

    def _make_book(self) -> OrderBook:
        p = self._tick_price()
        spread_pct = 0.001  # 0.1%
        bids = [(p * (1 - spread_pct * i), 1.0 + random.random() * 2)
                for i in range(20)]
        asks = [(p * (1 + spread_pct * i), 1.0 + random.random() * 2)
                for i in range(20)]
        return OrderBook(symbol=self._symbol, bids=bids, asks=asks)

    def _execute_fill(self, order: Order, fill_price: float) -> None:
        fee = order.quantity * fill_price * self._fee_pct
        order.filled_qty = order.quantity
        order.avg_fill_price = fill_price
        order.fee = fee
        order.status = "filled"
        order.updated_at = time.time()
        # Update paper balance
        if order.side == "buy":
            cost = order.quantity * fill_price + fee
            self._balance.quote -= cost
            self._balance.base += order.quantity
        else:
            proceeds = order.quantity * fill_price - fee
            self._balance.base -= order.quantity
            self._balance.quote += proceeds

    # ── Public API ────────────────────────────────────────────────────────────

    def get_order_book(self, symbol: str, depth: int = 20) -> OrderBook:
        return self._make_book()

    def get_balance(self) -> Balance:
        return Balance(base=self._balance.base, quote=self._balance.quote)

    def place_order(self, symbol: str, side: str, order_type: str,
                    quantity: float, price: Optional[float] = None) -> Order:
        order_id = str(uuid.uuid4())
        fill_price = price if (order_type == "limit" and price) else self._tick_price()
        order = Order(
            order_id=order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=fill_price,
        )
        self._orders[order_id] = order

        # Simulate fill
        if random.random() <= self._fill_probability:
            self._execute_fill(order, fill_price)
            logger.debug(f"[Paper] Order {order_id} filled  price={fill_price:.2f}")
        else:
            logger.debug(f"[Paper] Order {order_id} left open (fill_prob miss)")

        return order

    def get_order(self, order_id: str, symbol: str) -> Order:
        if order_id not in self._orders:
            raise ValueError(f"Order {order_id} not found in paper book")
        return self._orders[order_id]

    def cancel_order(self, order_id: str, symbol: str) -> bool:
        order = self._orders.get(order_id)
        if order and order.status == "open":
            order.status = "cancelled"
            order.updated_at = time.time()
            logger.debug(f"[Paper] Order {order_id} cancelled")
            return True
        return False

    def get_fee_rate(self, order_type: str = "taker") -> float:
        return self._fee_pct


# ── Live trading client ───────────────────────────────────────────────────────

class LiveExchangeClient(ExchangeClient):
    """
    Production-grade wrapper around ccxt.
    Requires:  pip install ccxt
    ENV VARS:  EXCHANGE_API_KEY  /  EXCHANGE_API_SECRET  /  EXCHANGE_PASSWORD
    """

    def __init__(self, cfg: dict) -> None:
        try:
            import ccxt  # type: ignore
        except ImportError:
            raise ImportError("Install ccxt first:  pip install ccxt")

        ex_cfg = cfg.get("exchange", {})
        name = ex_cfg.get("name", "binance")
        exchange_class = getattr(ccxt, name)
        self._ex = exchange_class({
            "apiKey": os.environ["EXCHANGE_API_KEY"],
            "secret": os.environ["EXCHANGE_API_SECRET"],
            "password": os.environ.get("EXCHANGE_PASSWORD", ""),
            "enableRateLimit": True,
        })
        self._ex.load_markets()
        self._symbol = ex_cfg.get("symbol", "BTC/USDT")
        fee_cfg = cfg.get("fees", {})
        self._taker_fee = float(fee_cfg.get("taker_pct", 0.10)) / 100.0
        self._maker_fee = float(fee_cfg.get("maker_pct", 0.08)) / 100.0
        logger.info(f"[Live] Connected to {name}  symbol={self._symbol}")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _map_order(self, raw: dict) -> Order:
        status_map = {
            "open": "open", "closed": "filled",
            "canceled": "cancelled", "cancelled": "cancelled",
            "partially_filled": "partially_filled",
        }
        filled = float(raw.get("filled") or 0.0)
        amount = float(raw.get("amount") or 0.0)
        if 0 < filled < amount:
            status = "partially_filled"
        else:
            status = status_map.get(raw.get("status", "open"), "open")

        fee_info = raw.get("fee") or {}
        fee = float(fee_info.get("cost") or 0.0)

        return Order(
            order_id=str(raw["id"]),
            symbol=raw.get("symbol", self._symbol),
            side=raw.get("side", ""),
            order_type=raw.get("type", "market"),
            quantity=amount,
            price=raw.get("price"),
            status=status,
            filled_qty=filled,
            avg_fill_price=float(raw.get("average") or raw.get("price") or 0.0),
            fee=fee,
            created_at=raw.get("timestamp", 0) / 1000.0,
            updated_at=time.time(),
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def get_order_book(self, symbol: str, depth: int = 20) -> OrderBook:
        raw = self._ex.fetch_order_book(symbol, depth)
        return OrderBook(
            symbol=symbol,
            bids=[(float(p), float(q)) for p, q in raw["bids"]],
            asks=[(float(p), float(q)) for p, q in raw["asks"]],
            timestamp=raw.get("timestamp", time.time() * 1000) / 1000.0,
        )

    def get_balance(self) -> Balance:
        raw = self._ex.fetch_balance()
        base, quote = self._symbol.split("/")
        return Balance(
            base=float(raw.get("free", {}).get(base, 0.0)),
            quote=float(raw.get("free", {}).get(quote, 0.0)),
        )

    def place_order(self, symbol: str, side: str, order_type: str,
                    quantity: float, price: Optional[float] = None) -> Order:
        params: dict = {}
        raw = self._ex.create_order(symbol, order_type, side, quantity, price, params)
        return self._map_order(raw)

    def get_order(self, order_id: str, symbol: str) -> Order:
        raw = self._ex.fetch_order(order_id, symbol)
        return self._map_order(raw)

    def cancel_order(self, order_id: str, symbol: str) -> bool:
        try:
            self._ex.cancel_order(order_id, symbol)
            return True
        except Exception as exc:
            logger.error(f"Failed to cancel order {order_id}", exc)
            return False

    def get_fee_rate(self, order_type: str = "taker") -> float:
        return self._taker_fee if order_type == "taker" else self._maker_fee


# ── Factory ───────────────────────────────────────────────────────────────────

def build_client(cfg: dict) -> ExchangeClient:
    mode = cfg.get("mode", "paper")
    if mode == "paper":
        return PaperExchangeClient(cfg)
    elif mode == "live":
        return LiveExchangeClient(cfg)
    else:
        raise ValueError(f"Unknown mode: {mode!r}  (expected 'paper' or 'live')")
