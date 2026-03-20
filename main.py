"""
Wash Trade Detector
===================
Identifies wash trades in a trade dataset.

A wash trade occurs when the same party (or closely related parties) appears
on both sides of a transaction, creating artificial volume without a genuine
change in ownership or economic risk.

Usage:
    python main.py --input trades.csv --output results.csv --window 60

CSV format expected (trades.csv):
    trade_id, timestamp, buyer_id, seller_id, asset, quantity, price
"""

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    trade_id: str
    timestamp: datetime
    buyer_id: str
    seller_id: str
    asset: str
    quantity: float
    price: float


@dataclass
class WashTradeGroup:
    """A cluster of trades flagged as potential wash trades."""
    trades: List[Trade] = field(default_factory=list)

    @property
    def total_volume(self) -> float:
        return sum(t.quantity * t.price for t in self.trades)

    @property
    def assets(self) -> set:
        return {t.asset for t in self.trades}

    @property
    def participants(self) -> set:
        ids = set()
        for t in self.trades:
            ids.add(t.buyer_id)
            ids.add(t.seller_id)
        return ids


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_trades(filepath: str) -> List[Trade]:
    """Load trades from a CSV file."""
    trades: List[Trade] = []
    with open(filepath, newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                trade = Trade(
                    trade_id=row["trade_id"].strip(),
                    timestamp=datetime.fromisoformat(row["timestamp"].strip()),
                    buyer_id=row["buyer_id"].strip(),
                    seller_id=row["seller_id"].strip(),
                    asset=row["asset"].strip(),
                    quantity=float(row["quantity"]),
                    price=float(row["price"]),
                )
                trades.append(trade)
            except (KeyError, ValueError) as exc:
                print(f"[WARN] Skipping malformed row {row}: {exc}", file=sys.stderr)
    return trades


# ---------------------------------------------------------------------------
# Detection logic
# ---------------------------------------------------------------------------

def detect_self_trades(trades: List[Trade]) -> List[WashTradeGroup]:
    """Flag trades where buyer_id == seller_id (direct self-trade)."""
    groups: List[WashTradeGroup] = []
    for t in trades:
        if t.buyer_id == t.seller_id:
            groups.append(WashTradeGroup(trades=[t]))
    return groups


def detect_round_trip_trades(
    trades: List[Trade],
    window_seconds: int = 60,
) -> List[WashTradeGroup]:
    """
    Flag pairs of opposing trades between the same two parties for the same
    asset within a rolling time window.

    A round-trip looks like:
        Party A buys from Party B, then Party B buys from Party A
        (or vice versa) within `window_seconds`.
    """
    window = timedelta(seconds=window_seconds)
    # Group trades by (frozenset of parties, asset)
    key_to_trades: dict = defaultdict(list)
    for t in trades:
        key = (frozenset([t.buyer_id, t.seller_id]), t.asset)
        key_to_trades[key].append(t)

    groups: List[WashTradeGroup] = []
    for (parties, asset), bucket in key_to_trades.items():
        # Sort by time so we can do a sliding-window comparison
        bucket.sort(key=lambda x: x.timestamp)
        for i, t1 in enumerate(bucket):
            for t2 in bucket[i + 1 :]:
                if t2.timestamp - t1.timestamp > window:
                    break
                # Opposing directions: buyer/seller roles are swapped
                if t1.buyer_id == t2.seller_id and t1.seller_id == t2.buyer_id:
                    groups.append(WashTradeGroup(trades=[t1, t2]))
    return groups


def detect_wash_trades(
    trades: List[Trade],
    window_seconds: int = 60,
) -> List[WashTradeGroup]:
    """Run all detection strategies and deduplicate results."""
    seen_ids: set = set()
    all_groups: List[WashTradeGroup] = []

    candidate_groups = detect_self_trades(trades) + detect_round_trip_trades(
        trades, window_seconds
    )

    for group in candidate_groups:
        # Use a frozenset of trade IDs as the dedup key
        key = frozenset(t.trade_id for t in group.trades)
        if key not in seen_ids:
            seen_ids.add(key)
            all_groups.append(group)

    return all_groups


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_summary(groups: List[WashTradeGroup]) -> None:
    if not groups:
        print("No wash trades detected.")
        return

    print(f"\n{'='*60}")
    print(f"  WASH TRADE REPORT — {len(groups)} group(s) detected")
    print(f"{'='*60}")
    for idx, group in enumerate(groups, 1):
        print(f"\nGroup {idx}:")
        print(f"  Participants : {', '.join(sorted(group.participants))}")
        print(f"  Assets       : {', '.join(sorted(group.assets))}")
        print(f"  Trades       : {len(group.trades)}")
        print(f"  Total Volume : {group.total_volume:,.2f}")
        for t in group.trades:
            print(
                f"    [{t.trade_id}] {t.timestamp.isoformat()}  "
                f"{t.buyer_id} <-> {t.seller_id}  "
                f"{t.asset}  qty={t.quantity}  price={t.price}"
            )
    print(f"\n{'='*60}\n")


def write_results(groups: List[WashTradeGroup], output_path: str) -> None:
    """Write flagged trades to a CSV file."""
    fieldnames = [
        "group_id",
        "trade_id",
        "timestamp",
        "buyer_id",
        "seller_id",
        "asset",
        "quantity",
        "price",
    ]
    with open(output_path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for idx, group in enumerate(groups, 1):
            for t in group.trades:
                writer.writerow(
                    {
                        "group_id": idx,
                        "trade_id": t.trade_id,
                        "timestamp": t.timestamp.isoformat(),
                        "buyer_id": t.buyer_id,
                        "seller_id": t.seller_id,
                        "asset": t.asset,
                        "quantity": t.quantity,
                        "price": t.price,
                    }
                )
    print(f"Results written to {output_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect wash trades in a CSV trade dataset."
    )
    parser.add_argument(
        "--input", "-i", required=True, help="Path to input trades CSV file."
    )
    parser.add_argument(
        "--output", "-o", default=None, help="Path to output CSV file (optional)."
    )
    parser.add_argument(
        "--window",
        "-w",
        type=int,
        default=60,
        help="Rolling time window in seconds for round-trip detection (default: 60).",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    print(f"Loading trades from: {args.input}")
    trades = parse_trades(args.input)
    print(f"  Loaded {len(trades)} trade(s).")

    print(f"Running wash-trade detection (window={args.window}s) …")
    groups = detect_wash_trades(trades, window_seconds=args.window)

    print_summary(groups)

    if args.output:
        write_results(groups, args.output)

    return 0 if not groups else 1


if __name__ == "__main__":
    sys.exit(main())
