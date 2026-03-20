# wash-trade

Compliant crypto trading bot — buy on signal, wait 30 s, sell.

---

## Strategy

1. Poll `get_buy_signal()` every N seconds.
2. When a signal fires, run every risk check (balance, liquidity, slippage, fees, daily-loss, rate limits).
3. Place a **market BUY** for the approved quantity.
4. Wait for fill confirmation (polls exchange; cancels stale orders).
5. Start a **30-second timer** — counted from the actual fill timestamp, not submission.
6. Place a **market SELL** for the exact filled quantity.
7. Handle partial sells: keep re-selling the remainder until flat.
8. Log signal time, order submission, fill, sell, quantity, fees, and realised PnL.

---

## Project structure

```
.
├── config.yaml          # all tunable parameters
├── main.py              # entry point
├── exchange_client.py   # PaperExchangeClient + LiveExchangeClient (ccxt)
├── risk_manager.py      # pre-trade and post-trade risk checks
├── execution.py         # order placement, fill polling, sell timer
├── strategy.py          # get_buy_signal() hook + trade cycle orchestration
├── logger.py            # structured JSON events + human-readable logs
├── requirements.txt
└── tests/
    ├── test_risk_manager.py
    ├── test_execution.py
    └── test_strategy.py
```

---

## Quick start

### 1. Install dependencies

```bash
pip install pyyaml          # always required
pip install ccxt            # only for live trading
```

### 2. Paper trading (no API keys needed)

```bash
python main.py              # runs forever, polls for signals
python main.py --once       # single signal poll then exits
```

### 3. Live trading

```bash
export EXCHANGE_API_KEY="your_key"
export EXCHANGE_API_SECRET="your_secret"
export MODE=live
python main.py
```

> **Warning:** Live mode places real orders with real money. Always paper-trade and review logs before switching.

### 4. Override config

```bash
python main.py --config my_config.yaml
```

### 5. Run tests

```bash
python -m pytest tests/ -v
# or without pytest:
python -m unittest discover tests/
```

---

## Configuration reference (`config.yaml`)

| Key | Default | Description |
|-----|---------|-------------|
| `mode` | `paper` | `paper` or `live` |
| `exchange.name` | `binance` | ccxt exchange id |
| `exchange.symbol` | `BTC/USDT` | trading pair |
| `strategy.sell_delay_seconds` | `30` | seconds to wait after buy fill |
| `strategy.signal_poll_interval_seconds` | `5` | how often to call `get_buy_signal()` |
| `execution.order_type` | `market` | `market` or `limit` |
| `execution.fill_timeout_seconds` | `60` | cancel unfilled order after this |
| `execution.sell_retry_attempts` | `5` | retry sell this many times |
| `risk.max_position_size_usd` | `500` | max single-trade notional |
| `risk.max_daily_loss_usd` | `200` | bot pauses if daily loss exceeds this |
| `risk.max_trades_per_hour` | `10` | rate limit |
| `risk.consecutive_losses_threshold` | `3` | trigger cooldown after N losses |
| `risk.consecutive_loss_cooldown_seconds` | `300` | cooldown duration |
| `risk.min_balance_usd` | `50` | refuse to trade below this |
| `risk.min_order_book_depth_usd` | `10000` | minimum order book depth (5 levels) |
| `risk.max_slippage_pct` | `0.30` | reject if estimated market slippage exceeds |
| `risk.max_fee_pct` | `0.20` | reject if round-trip fees exceed this % |
| `fees.taker_pct` | `0.10` | taker fee in percent |

---

## Replacing the signal function

Edit `strategy.py` → `get_buy_signal()`:

```python
def get_buy_signal(symbol: str, order_book: OrderBook) -> Optional[BuySignal]:
    # Your logic here: websocket event, REST response, ML model output, etc.
    if my_indicator_fires():
        return BuySignal(symbol=symbol, suggested_quantity_base=0.001)
    return None
```

The function receives the live order book so signals can be price-aware.

---

## Fee and slippage risks

### Fees
- Every round-trip costs `2 × taker_fee × trade_value`.
- At Binance's standard 0.10% taker fee, a $500 trade costs ~$1.00 in fees each cycle.
- Configure `risk.max_fee_pct` to auto-reject if fees exceed a threshold.

### Slippage
- Market orders fill at the best available price, which may differ from the mid-price.
- The risk manager **walks the order book** before placing an order to estimate fill price.
- If estimated slippage > `risk.max_slippage_pct`, the trade is rejected.
- Thin books (low `min_order_book_depth_usd`) are rejected outright.

### Combined P&L
Each trade must clear:

```
realised_pnl = (sell_price - buy_price) × qty − buy_fee − sell_fee
```

A 30-second hold offers very little time for price movement to offset fees + spread.
Size positions conservatively and monitor `daily_pnl` in the logs.

---

## Logs

Two log files are written:

| File | Content |
|------|---------|
| `bot.log` | Human-readable timestamped lines |
| `bot_events.jsonl` | One JSON object per event (ingestible by ELK / Datadog) |

Key event types: `signal_received`, `order_submitted`, `order_filled`, `sell_timer_started`, `trade_completed`, `risk_rejected`, `order_cancelled`, `error`.

---

## Compliance notes

- No self-trades, wash trades, or spoofing — each BUY is driven by an external signal and each SELL is a genuine close of that position.
- `allow_overlapping_positions: false` (default) enforces one-position-at-a-time.
- All orders are real-intent orders with immediate economic purpose.
