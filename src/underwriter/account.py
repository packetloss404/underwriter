"""Reading account state and raw positions from the broker.

The one place that turns Alpaca's account and position responses into the
shapes the rest of the agent reasons about. Everything here is a read; nothing
in this module can place, cancel or modify an order.

Positions come back as individual option contracts and are handed on exactly
that way. Pairing them into spreads is `positions.reassemble_spreads`, which
needs the journal to do it correctly -- so this module deliberately does not
try, rather than guessing at a grouping and being confidently wrong.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from underwriter.positions import RawOptionPosition

# Alpaca reports option positions with this asset class. Equity positions would
# come back alongside them if we ever held any; the strategy does not, and
# filtering here means a stray share position cannot be mistaken for a leg.
OPTION_ASSET_CLASS = "us_option"


class TradingApiLike(Protocol):
    """The subset of the trading client this module uses. Reads only."""

    def get_account(self) -> Any: ...
    def get_all_positions(self) -> Any: ...
    def get_clock(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class Account:
    """Account state, in the shape preflight and the cycle expect.

    Field names mirror Alpaca's own so the mapping stays obvious, and the
    numeric fields keep their string-or-number ambiguity rather than being
    coerced here: `preflight` already parses them defensively, and parsing in
    two places is how the two disagree.
    """

    status: object
    trading_blocked: bool
    account_blocked: bool
    equity: object
    options_trading_level: object
    options_approved_level: object
    options_buying_power: object
    last_equity: object
    cash: object = None
    buying_power: object = None


def _positions_of(raw: object) -> Sequence[Any]:
    """Alpaca returns a list; a raw-data client returns something list-like.

    An unrecognised shape yields nothing rather than raising, because the
    position read must not be the thing that takes the cycle down -- an empty
    book is visible to the snapshot diff, an exception is not.
    """
    if raw is None:
        return ()
    if isinstance(raw, Sequence) and not isinstance(raw, str | bytes):
        return raw
    if isinstance(raw, Iterable):
        return tuple(raw)
    return ()


def to_raw_option_position(position: Any) -> RawOptionPosition | None:
    """Map one Alpaca position, or None if it is not an option we can read.

    `qty` is normalised to be SIGNED -- negative for short. Alpaca reports the
    magnitude in `qty` and the direction separately in `side`, and a short leg
    recorded as positive would pair as a long one and invert the whole spread.
    """
    symbol = getattr(position, "symbol", None)
    if not symbol:
        return None

    asset_class = str(getattr(position, "asset_class", "") or "")
    if asset_class and OPTION_ASSET_CLASS not in asset_class.lower():
        return None

    try:
        qty = float(str(getattr(position, "qty", "")))
    except (TypeError, ValueError):
        return None

    side = str(getattr(position, "side", "") or "").lower()
    if "short" in side:
        qty = -abs(qty)
    elif "long" in side:
        qty = abs(qty)

    def _optional_float(name: str) -> float | None:
        try:
            value = getattr(position, name, None)
            return None if value is None else float(str(value))
        except (TypeError, ValueError):
            return None

    return RawOptionPosition(
        symbol=str(symbol),
        qty=qty,
        avg_entry_price=_optional_float("avg_entry_price"),
        current_price=_optional_float("current_price"),
    )


class AlpacaBroker:
    """Reads account state and positions. Satisfies `cycle.Broker`."""

    def __init__(self, client: TradingApiLike) -> None:
        self._client = client

    def account(self) -> Account:
        raw = self._client.get_account()
        return Account(
            status=getattr(raw, "status", None),
            trading_blocked=bool(getattr(raw, "trading_blocked", False)),
            account_blocked=bool(getattr(raw, "account_blocked", False)),
            equity=getattr(raw, "equity", None),
            options_trading_level=getattr(raw, "options_trading_level", None),
            options_approved_level=getattr(raw, "options_approved_level", None),
            options_buying_power=getattr(raw, "options_buying_power", None),
            last_equity=getattr(raw, "last_equity", None),
            cash=getattr(raw, "cash", None),
            buying_power=getattr(raw, "buying_power", None),
        )

    def clock(self) -> Any:
        """The market clock, as the broker reports it."""
        return self._client.get_clock()

    def positions(self) -> tuple[RawOptionPosition, ...]:
        """Option positions as individual contracts.

        A position we cannot read is SKIPPED, not guessed at -- but that means
        it disappears from the book, so callers must treat a shrinking position
        count as a signal rather than as good news. The snapshot diff will see
        it as a departure and file it for attention.
        """
        mapped = (
            to_raw_option_position(p) for p in _positions_of(self._client.get_all_positions())
        )
        return tuple(p for p in mapped if p is not None)


def paper_broker(api_key: str, secret_key: str) -> AlpacaBroker:
    """Construct a reader against the paper account.

    `paper=True` is not a configuration choice and there is no branch that can
    flip it, matching the guarantee in `config.Settings.trading_host`.
    """
    from alpaca.trading.client import TradingClient

    return AlpacaBroker(TradingClient(api_key=api_key, secret_key=secret_key, paper=True))
