"""Backtesting entry point for the EdgeX/Lighter arbitrage strategy.

This module replays logged BBO data and applies the same spread-based
open/close logic used by the live bot to generate a 30-day equity curve
and traded volume profile.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


def _parse_decimal(value: str, *, allow_zero: bool) -> Decimal:
    """Parse a Decimal and enforce non-negative constraints."""
    try:
        parsed_value = Decimal(value)
    except Exception as exc:  # pylint: disable=broad-except
        raise argparse.ArgumentTypeError(f"Invalid decimal value: '{value}'") from exc

    if parsed_value < 0 or (not allow_zero and parsed_value == 0):
        zero_text = " or equal to zero" if allow_zero else ""
        raise argparse.ArgumentTypeError(
            f"Value must be greater than{zero_text} zero"
        )

    return parsed_value


@dataclass
class BBORecord:
    """Single snapshot of best bid/ask data for both venues."""

    timestamp: datetime
    maker_bid: Decimal
    maker_ask: Decimal
    lighter_bid: Decimal
    lighter_ask: Decimal
    long_signal: Optional[bool]
    short_signal: Optional[bool]
    long_threshold: Decimal
    short_threshold: Decimal


@dataclass
class BacktestResult:
    """Summary of backtest outputs."""

    equity_curve: List[Tuple[datetime, Decimal]]
    volume_curve: List[Tuple[datetime, Decimal]]
    final_equity: Decimal
    total_volume: Decimal
    trades_executed: int


class ArbitrageBacktester:
    """Replay historical BBO data with the same entry logic as the live bot."""

    def __init__(
        self,
        data_path: Path,
        order_quantity: Decimal,
        max_position: Decimal,
        long_threshold: Decimal,
        short_threshold: Decimal,
        initial_capital: Decimal,
        output_dir: Path,
    ) -> None:
        self.data_path = data_path
        self.order_quantity = order_quantity
        self.max_position = max_position
        self.long_threshold = long_threshold
        self.short_threshold = short_threshold
        self.initial_capital = initial_capital
        self.output_dir = output_dir

        self.output_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _parse_bool(raw: str | None) -> Optional[bool]:
        if raw is None or raw == "":
            return None
        lowered = raw.strip().lower()
        if lowered in {"true", "1", "t", "yes", "y"}:
            return True
        if lowered in {"false", "0", "f", "no", "n"}:
            return False
        return None

    def _load_records(self) -> List[BBORecord]:
        """Load BBO snapshots from a CSV file produced by DataLogger."""
        records: List[BBORecord] = []
        with self.data_path.open(newline="") as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                try:
                    timestamp = datetime.fromisoformat(row["timestamp"])
                    long_threshold = Decimal(row.get("long_maker_threshold", self.long_threshold))
                    short_threshold = Decimal(row.get("short_maker_threshold", self.short_threshold))
                    records.append(
                        BBORecord(
                            timestamp=timestamp,
                            maker_bid=Decimal(row.get("maker_bid", "0")),
                            maker_ask=Decimal(row.get("maker_ask", "0")),
                            lighter_bid=Decimal(row.get("lighter_bid", "0")),
                            lighter_ask=Decimal(row.get("lighter_ask", "0")),
                            long_signal=self._parse_bool(row.get("long_maker")),
                            short_signal=self._parse_bool(row.get("short_maker")),
                            long_threshold=long_threshold,
                            short_threshold=short_threshold,
                        )
                    )
                except Exception:
                    # Skip malformed rows but continue processing the rest.
                    continue
        return records

    def _filter_recent_records(self, records: Iterable[BBORecord]) -> List[BBORecord]:
        """Keep only the most recent 30 days of data."""
        record_list = list(records)
        if not record_list:
            return []

        latest_time = max(record.timestamp for record in record_list)
        cutoff = latest_time - timedelta(days=30)
        return [record for record in record_list if record.timestamp >= cutoff]

    def _should_go_long(self, record: BBORecord) -> bool:
        if record.long_signal is not None:
            return record.long_signal

        threshold = record.long_threshold or self.long_threshold
        return (
            record.lighter_bid > 0
            and record.maker_bid > 0
            and record.lighter_bid - record.maker_bid > threshold
        )

    def _should_go_short(self, record: BBORecord) -> bool:
        if record.short_signal is not None:
            return record.short_signal

        threshold = record.short_threshold or self.short_threshold
        return (
            record.maker_ask > 0
            and record.lighter_ask > 0
            and record.maker_ask - record.lighter_ask > threshold
        )

    def run(self) -> BacktestResult:
        """Execute the backtest and persist equity/volume curves."""
        raw_records = self._load_records()
        records = self._filter_recent_records(raw_records)
        records.sort(key=lambda r: r.timestamp)

        equity_curve: List[Tuple[datetime, Decimal]] = []
        volume_curve: List[Tuple[datetime, Decimal]] = []
        trades_executed = 0

        equity = self.initial_capital
        cumulative_volume = Decimal("0")
        edgex_position = Decimal("0")

        for record in records:
            long_signal = self._should_go_long(record)
            short_signal = self._should_go_short(record)

            executed = False
            if long_signal and edgex_position < self.max_position:
                # Buy on EdgeX (maker ask) and sell on Lighter (bid)
                if record.maker_ask > 0 and record.lighter_bid > 0:
                    pnl = (record.lighter_bid - record.maker_ask) * self.order_quantity
                    equity += pnl
                    edgex_position += self.order_quantity
                    executed = True
            elif short_signal and edgex_position > -self.max_position:
                # Sell on EdgeX (maker bid) and buy on Lighter (ask)
                if record.maker_bid > 0 and record.lighter_ask > 0:
                    pnl = (record.maker_bid - record.lighter_ask) * self.order_quantity
                    equity += pnl
                    edgex_position -= self.order_quantity
                    executed = True

            if executed:
                trades_executed += 1
                cumulative_volume += self.order_quantity * Decimal("2")
                equity_curve.append((record.timestamp, equity))
                volume_curve.append((record.timestamp, cumulative_volume))

        self._save_curve(
            self.output_dir / "equity_curve.csv",
            ["timestamp", "equity"],
            equity_curve,
        )
        self._save_curve(
            self.output_dir / "volume_curve.csv",
            ["timestamp", "cumulative_base_volume"],
            volume_curve,
        )

        return BacktestResult(
            equity_curve=equity_curve,
            volume_curve=volume_curve,
            final_equity=equity,
            total_volume=cumulative_volume,
            trades_executed=trades_executed,
        )

    def _save_curve(
        self, path: Path, headers: List[str], data: List[Tuple[datetime, Decimal]]
    ) -> None:
        with path.open("w", newline="") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(headers)
            for timestamp, value in data:
                writer.writerow([timestamp.isoformat(), f"{value:.8f}"])


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backtest the EdgeX/Lighter arbitrage strategy using BBO CSV data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--data",
        type=Path,
        required=True,
        help="Path to BBO CSV data produced by the live bot (logs/<exchange>_<ticker>_bbo_data.csv)",
    )
    parser.add_argument(
        "--size",
        type=lambda v: _parse_decimal(v, allow_zero=False),
        required=True,
        help="Order size per leg (base asset units)",
    )
    parser.add_argument(
        "--max-position",
        type=lambda v: _parse_decimal(v, allow_zero=True),
        default=Decimal("0"),
        help="Maximum EdgeX position to carry during backtest (default: 0 for flat)",
    )
    parser.add_argument(
        "--long-threshold",
        type=lambda v: _parse_decimal(v, allow_zero=False),
        default=Decimal("10"),
        help="Minimum spread to trigger a long EdgeX leg",
    )
    parser.add_argument(
        "--short-threshold",
        type=lambda v: _parse_decimal(v, allow_zero=False),
        default=Decimal("10"),
        help="Minimum spread to trigger a short EdgeX leg",
    )
    parser.add_argument(
        "--initial-capital",
        type=lambda v: _parse_decimal(v, allow_zero=False),
        default=Decimal("100000"),
        help="Starting capital for the equity curve (quote currency)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("logs/backtests"),
        help="Directory to store equity and volume CSV outputs",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()

    backtester = ArbitrageBacktester(
        data_path=args.data,
        order_quantity=args.size,
        max_position=args.max_position,
        long_threshold=args.long_threshold,
        short_threshold=args.short_threshold,
        initial_capital=args.initial_capital,
        output_dir=args.output_dir,
    )

    result = backtester.run()

    print("Backtest complete")
    print(f"Trades executed: {result.trades_executed}")
    print(f"Final equity: {result.final_equity}")
    print(f"Total base volume: {result.total_volume}")
    print(f"Equity curve saved to: {args.output_dir / 'equity_curve.csv'}")
    print(f"Volume curve saved to: {args.output_dir / 'volume_curve.csv'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
