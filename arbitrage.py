import argparse
import asyncio
import sys
from decimal import Decimal

import dotenv

from strategy.edgex_arb import EdgexArb


def _parse_positive_int(value: str) -> int:
    """Parse a positive integer for timeout arguments."""
    try:
        parsed_value = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid integer value: '{value}'") from exc

    if parsed_value <= 0:
        raise argparse.ArgumentTypeError("Value must be greater than zero")

    return parsed_value


def _parse_decimal(value: str, *, allow_zero: bool) -> Decimal:
    """Parse a Decimal and enforce non-negative constraints."""
    try:
        parsed_value = Decimal(value)
    except Exception as exc:
        raise argparse.ArgumentTypeError(f"Invalid decimal value: '{value}'") from exc

    if parsed_value < 0 or (not allow_zero and parsed_value == 0):
        zero_text = " or equal to zero" if allow_zero else ""
        raise argparse.ArgumentTypeError(
            f"Value must be greater than{zero_text} zero"
        )

    return parsed_value


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Cross-Exchange Arbitrage Bot Entry Point",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--exchange",
        type=str,
        default="edgex",
        help="Exchange to use (edgex)",
    )
    parser.add_argument(
        "--ticker",
        type=str,
        default="BTC",
        help="Ticker symbol (default: BTC)",
    )
    parser.add_argument(
        "--size",
        type=lambda v: _parse_decimal(v, allow_zero=False),
        required=True,
        help="Number of tokens to buy/sell per order",
    )
    parser.add_argument(
        "--fill-timeout",
        type=_parse_positive_int,
        default=5,
        help="Timeout in seconds for maker order fills (default: 5)",
    )
    parser.add_argument(
        "--max-position",
        type=lambda v: _parse_decimal(v, allow_zero=True),
        default=Decimal("0"),
        help="Maximum position to hold (default: 0)",
    )
    parser.add_argument(
        "--long-threshold",
        type=lambda v: _parse_decimal(v, allow_zero=False),
        default=Decimal("10"),
        help="Long threshold for edgeX (default: 10)",
    )
    parser.add_argument(
        "--short-threshold",
        type=lambda v: _parse_decimal(v, allow_zero=False),
        default=Decimal("10"),
        help="Short threshold for edgeX (default: 10)",
    )
    return parser.parse_args()


def validate_exchange(exchange):
    """Validate that the exchange is supported."""
    supported_exchanges = ['edgex']
    if exchange.lower() not in supported_exchanges:
        print(f"Error: Unsupported exchange '{exchange}'")
        print(f"Supported exchanges: {', '.join(supported_exchanges)}")
        sys.exit(1)


async def main():
    """Main entry point that creates and runs the cross-exchange arbitrage bot."""
    args = parse_arguments()

    dotenv.load_dotenv()

    # Validate exchange
    validate_exchange(args.exchange)

    try:
        bot = EdgexArb(
            ticker=args.ticker.upper(),
            order_quantity=args.size,
            fill_timeout=args.fill_timeout,
            max_position=args.max_position,
            long_ex_threshold=args.long_threshold,
            short_ex_threshold=args.short_threshold,
        )

        # Run the bot
        await bot.run()

    except KeyboardInterrupt:
        print("\nCross-Exchange Arbitrage interrupted by user")
        return 1
    except Exception as e:
        print(f"Error running cross-exchange arbitrage: {e}")
        import traceback
        print(f"Full traceback: {traceback.format_exc()}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
