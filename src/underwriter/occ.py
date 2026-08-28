"""OCC option symbol parsing.

Alpaca returns option chains as a dict keyed by OCC symbol, so getting from a
chain response to something we can reason about means parsing these. The format
is fixed-width from the right, which makes it unambiguous:

    SPY   260911 P 00637000
    root  YYMMDD C/P strike x 1000

Parsing from the right matters: roots vary in length (SPY, QQQ, BRK.B) but
every field after the root has a fixed width, so anchoring to the end is exact
where anchoring to the start is guesswork.

We parse rather than construct. Alpaca's contract endpoint is the source of
truth for which contracts exist; building an OCC symbol ourselves and hoping it
resolves is how you get silent empty chains.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

# 6 digits of date, one right indicator, 8 digits of strike.
_TAIL = re.compile(r"^(?P<root>[A-Z][A-Z0-9.]*?)(?P<ymd>\d{6})(?P<right>[CP])(?P<strike>\d{8})$")

STRIKE_SCALE = 1000


class OccParseError(ValueError):
    """The symbol is not a well-formed OCC option symbol."""


@dataclass(frozen=True, slots=True)
class OccSymbol:
    symbol: str
    root: str
    expiry: date
    is_call: bool
    strike: float

    @property
    def is_put(self) -> bool:
        return not self.is_call


def parse(symbol: str) -> OccSymbol:
    """Parse an OCC symbol, or raise.

    Raises rather than returning None because a chain key we cannot parse means
    our understanding of the response format is wrong, and silently dropping
    contracts would present as a thin chain -- which reads as a liquidity
    problem rather than a parsing bug.
    """
    match = _TAIL.match(symbol.strip().upper())
    if match is None:
        msg = f"not a well-formed OCC option symbol: {symbol!r}"
        raise OccParseError(msg)

    ymd = match.group("ymd")
    try:
        expiry = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
    except ValueError as exc:
        msg = f"OCC symbol {symbol!r} carries an impossible expiry {ymd!r}: {exc}"
        raise OccParseError(msg) from exc

    return OccSymbol(
        symbol=match.group(0),
        root=match.group("root"),
        expiry=expiry,
        is_call=match.group("right") == "C",
        strike=int(match.group("strike")) / STRIKE_SCALE,
    )
