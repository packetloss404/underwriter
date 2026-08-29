"""Market data access, and the conversion into domain objects.

Everything the strategy reasons about arrives through here: daily closes for
realised volatility and the regime filter, option chains for contract
selection, and the two implied-vol readings that form the term structure.

The conversion functions are pure and take already-fetched responses, so the
mapping logic is testable without credentials or a network. Only `MarketData`
touches Alpaca.

Two carried-through rules:

- **Never invent a value.** A snapshot without implied volatility yields a
  contract with `delta=None`, not an estimate. Downstream code already knows
  how to fall back or refuse.
- **A parse failure is loud.** A chain key we cannot read means our
  understanding of the response is wrong, and silently dropping it would look
  like a thin chain rather than a bug.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from underwriter.chain import Contract, ContractType, ExpiryWindow, Quote
from underwriter.occ import OccParseError
from underwriter.occ import parse as parse_occ
from underwriter.regime import TermStructure

log = logging.getLogger(__name__)

# Basic-plan option data is the indicative feed; the OPRA feed requires a paid
# plan we do not have. Naming it explicitly keeps the dashboard honest.
OPTION_FEED = "indicative"

# The Basic plan refuses consolidated SIP data from the last fifteen minutes
# with "subscription does not permit querying recent SIP data". Ending a query
# at `now` therefore fails outright rather than degrading. Backing the end off
# past the embargo keeps us on SIP, which is consolidated across every venue --
# the alternative, feed="iex", succeeds but covers only IEX's share of volume
# and prices differ (SPY closed 769.35 on SIP against 769.28 on IEX).
# See docs/GOTCHAS.md #5.
SIP_EMBARGO = timedelta(minutes=20)


class QuoteLike(Protocol):
    @property
    def bid_price(self) -> float | None: ...
    @property
    def ask_price(self) -> float | None: ...
    @property
    def timestamp(self) -> datetime: ...


class SnapshotLike(Protocol):
    @property
    def latest_quote(self) -> Any: ...
    @property
    def implied_volatility(self) -> float | None: ...
    @property
    def greeks(self) -> Any: ...


def quote_from(snapshot: SnapshotLike) -> Quote | None:
    """Build a quote, or None when the snapshot has no usable one.

    Returning None rather than a zeroed quote matters: the screening layer
    distinguishes "no quote" from "zero bid", and they mean different things.
    """
    raw = snapshot.latest_quote
    if raw is None:
        return None
    bid, ask = raw.bid_price, raw.ask_price
    if bid is None or ask is None:
        return None
    ts = raw.timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return Quote(bid=float(bid), ask=float(ask), as_of=ts)


def delta_from(snapshot: SnapshotLike) -> float | None:
    """Extract delta if the Basic plan supplied Greeks. Never computed here."""
    greeks = snapshot.greeks
    if greeks is None:
        return None
    delta = getattr(greeks, "delta", None)
    return None if delta is None else float(delta)


def contracts_from_chain(
    chain: Mapping[str, SnapshotLike],
    *,
    underlying: str,
    open_interest: Mapping[str, int] | None = None,
) -> list[Contract]:
    """Convert a chain response into domain contracts.

    `open_interest` is optional because the chain snapshot does not carry it --
    it lives only on the contracts endpoint, so populating it costs a second
    call and a join. See docs/GOTCHAS.md #4.
    """
    out: list[Contract] = []
    for symbol, snapshot in chain.items():
        try:
            occ = parse_occ(symbol)
        except OccParseError:
            log.warning("unparseable chain key %r for %s; skipping", symbol, underlying)
            continue
        out.append(
            Contract(
                symbol=occ.symbol,
                underlying=underlying,
                expiry=occ.expiry,
                strike=occ.strike,
                contract_type=ContractType.CALL if occ.is_call else ContractType.PUT,
                quote=quote_from(snapshot),
                delta=delta_from(snapshot),
                open_interest=None if open_interest is None else open_interest.get(occ.symbol),
            )
        )
    return out


def atm_implied_vol(
    chain: Mapping[str, SnapshotLike], *, underlying_price: float
) -> tuple[float, int] | None:
    """Implied vol of the contract nearest the money, with its days to expiry.

    Implied volatility lives on the chain *snapshot*, not on the contract, so
    this must be read from the raw response rather than from converted domain
    objects. Returns None when nothing in the slice carries one, which the
    Basic plan permits whenever a bid or ask is zero or the solver fails.
    """
    today = datetime.now(UTC).date()
    best: tuple[float, float, int] | None = None  # (distance, iv, dte)
    for symbol, snapshot in chain.items():
        iv = snapshot.implied_volatility
        if iv is None or iv <= 0:
            continue
        try:
            occ = parse_occ(symbol)
        except OccParseError:
            continue
        distance = abs(occ.strike - underlying_price)
        dte = (occ.expiry - today).days
        if best is None or distance < best[0]:
            best = (distance, float(iv), dte)
    return None if best is None else (best[1], best[2])


def term_structure_from(
    near_chain: Mapping[str, SnapshotLike],
    far_chain: Mapping[str, SnapshotLike],
    *,
    underlying_price: float,
) -> TermStructure | None:
    """Build the term structure from two expiry slices of the same underlying.

    Returns None when either slice lacks a usable implied volatility. The
    regime filter treats a missing curve as a block, so refusing to construct
    one here is the safe direction.
    """
    near = atm_implied_vol(near_chain, underlying_price=underlying_price)
    far = atm_implied_vol(far_chain, underlying_price=underlying_price)
    if near is None or far is None:
        return None
    return TermStructure(near_iv=near[0], far_iv=far[0], near_dte=near[1], far_dte=far[1])


def mids_from(snapshots: Mapping[str, SnapshotLike]) -> dict[str, float | None]:
    """Mid prices per contract, or None where unquotable.

    None rather than a default matters downstream: `exits.closing_debit` treats
    a missing quote as "cannot evaluate" rather than as a free exit, and a zero
    here would read as costless to close.
    """
    out: dict[str, float | None] = {}
    for symbol, snapshot in snapshots.items():
        quote = quote_from(snapshot)
        out[symbol] = None if quote is None or quote.mid <= 0 else quote.mid
    return out


def deltas_from(snapshots: Mapping[str, SnapshotLike]) -> dict[str, float | None]:
    """Deltas per contract. Never estimated; absent stays absent."""
    return {symbol: delta_from(snapshot) for symbol, snapshot in snapshots.items()}


@dataclass(frozen=True, slots=True)
class Bars:
    """Daily closes per symbol, oldest first."""

    closes: Mapping[str, list[float]]

    def for_symbol(self, symbol: str) -> list[float]:
        return list(self.closes.get(symbol, ()))


class MarketData:
    """Thin, credential-holding wrapper over the Alpaca data clients.

    Constructed only where credentials exist. Every method returns domain
    objects rather than SDK models, so the rest of the codebase never imports
    alpaca.
    """

    def __init__(self, api_key: str, secret_key: str) -> None:
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.historical.stock import StockHistoricalDataClient

        self._stock = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)
        self._option = OptionHistoricalDataClient(api_key=api_key, secret_key=secret_key)

    def daily_closes(
        self,
        symbols: Sequence[str],
        *,
        lookback_days: int = 90,
        feed: str | None = None,
    ) -> Bars:
        """Daily closes for the trailing window, oldest first.

        The window ends `SIP_EMBARGO` in the past so the Basic plan will serve
        consolidated data. This costs us nothing: realised volatility is
        computed from completed daily bars, and today's partial bar has no
        business in that calculation anyway.
        """
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        end = datetime.now(UTC) - SIP_EMBARGO
        request = StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=TimeFrame.Day,
            start=end - timedelta(days=lookback_days),
            end=end,
            **({"feed": feed} if feed else {}),
        )
        response = self._stock.get_stock_bars(request)
        raw = getattr(response, "data", response)
        return Bars(
            closes={
                symbol: [float(bar.close) for bar in bars] for symbol, bars in dict(raw).items()
            }
        )

    def chain(self, underlying: str, window: ExpiryWindow) -> Mapping[str, SnapshotLike]:
        """Raw chain slice for an explicit expiry window.

        The window is required, not optional. Omitting bounds silently returns
        only contracts expiring before the coming weekend. See GOTCHAS #1.
        """
        from alpaca.data.requests import OptionChainRequest

        request = OptionChainRequest(
            underlying_symbol=underlying,
            expiration_date_gte=window.gte,
            expiration_date_lte=window.lte,
        )
        return dict(self._option.get_option_chain(request))

    def contracts(self, underlying: str, window: ExpiryWindow) -> list[Contract]:
        """Domain contracts for an underlying across an expiry window."""
        return contracts_from_chain(self.chain(underlying, window), underlying=underlying)

    def option_snapshots(self, symbols: Sequence[str]) -> Mapping[str, SnapshotLike]:
        """Snapshots for named contracts, whatever their expiry.

        The exit path cannot reuse the scan's chains. A held position routinely
        sits outside the 5-14 day entry window -- it was opened days ago and has
        been decaying toward an expiry the current slice no longer covers -- so
        pricing it from those chains would silently return nothing and every
        exit trigger would read "cannot price" forever.

        An empty request returns an empty mapping without calling the API,
        since a snapshot request naming no symbols is an error rather than an
        empty answer.
        """
        from alpaca.data.requests import OptionSnapshotRequest

        wanted = list(symbols)
        if not wanted:
            return {}
        response = self._option.get_option_snapshot(OptionSnapshotRequest(symbol_or_symbols=wanted))
        return dict(response)

    def option_mids(self, symbols: Sequence[str]) -> dict[str, float | None]:
        """Mid prices for named contracts, or None where unquotable."""
        return mids_from(self.option_snapshots(symbols))

    def option_deltas(self, symbols: Sequence[str]) -> dict[str, float | None]:
        """Deltas for named contracts. Never estimated; absent stays absent."""
        return deltas_from(self.option_snapshots(symbols))


class OpenInterestSource:
    """Fetches open interest, which the chain snapshot does not carry.

    Separate from `MarketData` because it hits the trading API rather than the
    data API, and because it costs an extra call per underlying -- worth paying
    during calibration, not necessarily on every live cycle.
    """

    def __init__(self, api_key: str, secret_key: str) -> None:
        from alpaca.trading.client import TradingClient

        # paper=True is not a configuration choice here. There is no branch.
        self._trading = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)

    def for_underlying(
        self, underlying: str, window: ExpiryWindow, *, limit: int = 10_000
    ) -> dict[str, int]:
        from alpaca.trading.requests import GetOptionContractsRequest

        request = GetOptionContractsRequest(
            underlying_symbols=[underlying],
            expiration_date_gte=window.gte,
            expiration_date_lte=window.lte,
            limit=limit,
        )
        response = self._trading.get_option_contracts(request)
        contracts: Iterable[Any] = getattr(response, "option_contracts", []) or []
        return {
            c.symbol: int(c.open_interest)
            for c in contracts
            if getattr(c, "open_interest", None) is not None
        }
