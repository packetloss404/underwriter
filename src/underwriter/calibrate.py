"""Calibration: find out whether these thresholds ever fire.

Every number in this strategy -- the premium floor, the drawdown limit, the
expansion fraction, the curve ratio -- was written from research and has never
been tested against a real market. The largest practical risk to the project is
not a bad trade, it is an immaculate machine that stands down for four sessions
and posts a flat result.

This answers two questions with real data:

1. **Would the regime filter ever have permitted entry?** Computable across a
   long history, because it needs only daily closes.
2. **Is the premium floor plausible today?** Only computable as a snapshot,
   because implied volatility for a past date would require reconstructing the
   chain as it stood, which is a backtest rather than a calibration.

It also measures the option liquidity of the provisional instruments, which is
the evidence that promotes or drops them.

Nothing here places an order or mutates any state. It reads.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from underwriter import universe
from underwriter.chain import (
    Contract,
    ExpiryWindow,
    LiquidityPolicy,
    screen_contract,
)
from underwriter.config import RiskLimits, Settings
from underwriter.data import (
    MarketData,
    OpenInterestSource,
    atm_implied_vol,
    contracts_from_chain,
)
from underwriter.regime import RegimePolicy, RegimeVerdict, evaluate_regime
from underwriter.volatility import (
    Skipped,
    VolPolicy,
    VolRanking,
    rank_instrument,
    realised_volatility,
)

BENCHMARK = "SPY"


@dataclass(frozen=True, slots=True)
class SymbolSnapshot:
    """What we could actually see for one instrument, right now."""

    symbol: str
    provisional: bool
    close_count: int
    underlying_price: float | None
    chain_size: int
    near_count: int
    tradeable_near: int
    quoted: int
    with_iv: int
    with_delta: int
    with_open_interest: int
    median_spread_pct: float | None
    median_open_interest: float | None
    passing_liquidity: int
    ranking: VolRanking | Skipped | None = None

    @property
    def iv_coverage(self) -> float:
        return 0.0 if not self.chain_size else self.with_iv / self.chain_size

    @property
    def liquid_fraction(self) -> float:
        return 0.0 if not self.near_count else self.tradeable_near / self.near_count


@dataclass
class RegimeHistory:
    """How often the regime filter would have permitted entry, day by day."""

    sessions: int = 0
    permitted: int = 0
    blocks: dict[str, int] = field(default_factory=dict)

    @property
    def permitted_pct(self) -> float:
        return 0.0 if not self.sessions else self.permitted / self.sessions * 100

    def record(self, verdict: RegimeVerdict) -> None:
        self.sessions += 1
        if verdict.may_open:
            self.permitted += 1
        for reason in verdict.reasons:
            self.blocks[reason.value] = self.blocks.get(reason.value, 0) + 1


def replay_regime(
    benchmark_closes: Sequence[float],
    *,
    policy: RegimePolicy | None = None,
    warmup: int = 25,
) -> RegimeHistory:
    """Walk the benchmark history and ask the regime filter about each day.

    The term structure is waived here: it cannot be reconstructed for a past
    date without the chain as it stood, which is a backtest. Scheduled events
    are waived too, so the result measures the price-based gates in isolation
    -- which are the ones most likely to be miscalibrated.
    """
    policy = policy or RegimePolicy()
    history = RegimeHistory()
    for end in range(warmup, len(benchmark_closes) + 1):
        window = benchmark_closes[:end]
        verdict = evaluate_regime(
            benchmark_closes=window,
            expanding_flags=(),
            today=None,
            policy=policy,
            require_term_structure=False,
        )
        history.record(verdict)
    return history


# How far from spot a contract can sit and still be one we might plausibly
# trade. On a five-to-fourteen day tenor a 0.15-0.30 delta strike sits roughly
# one and a half to three percent out of the money, so four percent covers the
# short leg and its protective wing. This is the Greek-free equivalent of the
# delta band, used because the Basic plan omits delta on a large share of the
# chain. A wider band drags far-out-of-the-money junk into the median and made
# SPY read 7.9% when its at-the-money contract quotes about 2%.
NEAR_THE_MONEY_PCT = 4.0

# Minimum share of near-the-money contracts that must be tradeable before an
# instrument's premium ratio means anything.
#
# Implied volatility is solved from the mid. When bid and ask sit 56% apart the
# mid is fiction, so the implied vol is fiction, so the ratio is fiction --
# measured live, FXI showed the richest ratio in the universe at 1.61 with a
# 56.5% spread and not one tradeable strike. Ranking without this gate ranks
# spread width and calls it premium.
MIN_TRADEABLE_FRACTION = 0.20


def near_the_money(
    contracts: Sequence[Contract], *, spot: float, pct: float = NEAR_THE_MONEY_PCT
) -> list[Contract]:
    """The slice of a chain we would actually consider trading.

    Measuring spread and open interest across a whole chain is meaningless: a
    2,232-contract SPY chain is mostly far-out-of-the-money strikes with no
    quotes and no interest, and their medians say nothing about the contracts
    we would sell. SPY's median spread across the full chain reads 7.8% while
    the actual at-the-money contract quotes 3.86/3.94 -- about 2%.
    """
    if spot <= 0:
        return []
    band = spot * pct / 100
    return [c for c in contracts if abs(c.strike - spot) <= band]


@dataclass(frozen=True, slots=True)
class ChainStats:
    """Liquidity and data-coverage for one underlying's chain."""

    considered: int
    quoted: int
    with_delta: int
    with_open_interest: int
    median_spread_pct: float | None
    median_open_interest: float | None
    passing_liquidity: int
    passing_ignoring_staleness: int


def summarise_chain(
    contracts: Sequence[Contract],
    *,
    now: datetime,
    window: ExpiryWindow,
    policy: LiquidityPolicy,
) -> ChainStats:
    """Measure what the chain actually offers, before any strategy opinion.

    Two pass rates are reported. `passing_liquidity` applies the live rules
    exactly. `passing_ignoring_staleness` waives only the quote-age check,
    because outside market hours every quote is hours old and the live figure
    is uniformly zero -- correct behaviour, but it tells us nothing about
    whether the contracts are tradeable during a session.
    """
    relaxed = replace(policy, max_quote_age_seconds=float("inf"))
    spreads: list[float] = []
    ois: list[int] = []
    quoted = with_delta = with_oi = passing = passing_relaxed = 0

    for contract in contracts:
        if contract.quote is not None:
            quoted += 1
            if contract.quote.mid > 0:
                spreads.append(contract.quote.width_pct_of_mid)
        if contract.delta is not None:
            with_delta += 1
        if contract.open_interest is not None:
            with_oi += 1
            ois.append(contract.open_interest)
        # Screen against the contract's own type, so a chain of calls and puts
        # is measured fairly rather than half of it failing on WRONG_TYPE.
        if (
            screen_contract(
                contract,
                now=now,
                window=window,
                wanted=contract.contract_type,
                policy=policy,
            )
            is None
        ):
            passing += 1
        if (
            screen_contract(
                contract,
                now=now,
                window=window,
                wanted=contract.contract_type,
                policy=relaxed,
            )
            is None
        ):
            passing_relaxed += 1

    return ChainStats(
        considered=len(contracts),
        quoted=quoted,
        with_delta=with_delta,
        with_open_interest=with_oi,
        median_spread_pct=statistics.median(spreads) if spreads else None,
        median_open_interest=statistics.median(ois) if ois else None,
        passing_liquidity=passing,
        passing_ignoring_staleness=passing_relaxed,
    )


def _fmt_pct(value: float | None, width: int = 6) -> str:
    return "n/a".rjust(width) if value is None else f"{value:.1f}%".rjust(width)


def _fmt_num(value: float | None, width: int = 7) -> str:
    return "n/a".rjust(width) if value is None else f"{value:,.0f}".rjust(width)


def run(
    settings: Settings,
    *,
    lookback_days: int = 120,
    vol_policy: VolPolicy | None = None,
    liquidity: LiquidityPolicy | None = None,
    risk: RiskLimits | None = None,
) -> int:
    """Execute calibration and print the report. Returns a process exit code."""
    vol_policy = vol_policy or VolPolicy()
    risk = risk or settings.risk
    liquidity = liquidity or LiquidityPolicy(
        max_spread_pct_of_mid=risk.max_spread_pct_of_mid,
        min_open_interest=risk.min_open_interest,
        max_quote_age_seconds=risk.max_quote_age_seconds,
    )

    key = settings.alpaca_api_key.get_secret_value()
    secret = settings.alpaca_secret_key.get_secret_value()
    market = MarketData(key, secret)
    oi_source = OpenInterestSource(key, secret)

    symbols = universe.symbols(include_provisional=True)
    now = datetime.now(UTC)
    today = now.date()
    window = ExpiryWindow.from_dte(today, risk.min_days_to_expiry, risk.max_days_to_expiry)

    print(f"\nUNDERWRITER CALIBRATION  {now:%Y-%m-%d %H:%M} UTC")
    print(
        f"universe {len(symbols)} instruments "
        f"({len(universe.provisional_symbols())} provisional) · "
        f"expiry window {window.gte} to {window.lte}"
    )

    print("\nFetching daily closes...", flush=True)
    bars = market.daily_closes(symbols, lookback_days=lookback_days)

    # ---- 1. Would the regime filter ever permit entry? ----
    benchmark = bars.for_symbol(BENCHMARK)
    print(f"\n{'=' * 78}\nREGIME FILTER, replayed over {len(benchmark)} sessions of {BENCHMARK}")
    print("=" * 78)
    if len(benchmark) < 25:
        print(f"  insufficient history: {len(benchmark)} closes")
    else:
        history = replay_regime(benchmark)
        print(
            f"  entry permitted on {history.permitted} of {history.sessions} sessions "
            f"({history.permitted_pct:.0f}%)"
        )
        if history.blocks:
            print("  blocked by:")
            for reason, count in sorted(history.blocks.items(), key=lambda kv: -kv[1]):
                print(f"    {reason:28} {count:4}  ({count / history.sessions * 100:.0f}%)")
        if history.permitted_pct < 20:
            print("\n  WARNING: the regime filter permits entry on under a fifth of")
            print("  sessions. In a four-session window that is close to never trading.")

    # ---- 2. What does the premium look like right now? ----
    print(f"\n{'=' * 78}\nPREMIUM RANKING AND CHAIN LIQUIDITY, as of now")
    print("=" * 78)
    header = (
        f"  {'sym':5} {'p':1} {'closes':>6} {'chain':>6} {'IV%cov':>7} "
        f"{'spread':>7} {'OI':>7} {'liquid':>7}  {'RV':>6} {'IV':>6} {'ratio':>6}  verdict"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    snapshots: list[SymbolSnapshot] = []
    candidates = 0
    for symbol in symbols:
        closes = bars.for_symbol(symbol)
        spot = closes[-1] if closes else None
        try:
            raw_chain = market.chain(symbol, window)
            oi_map = oi_source.for_underlying(symbol, window)
            contracts = contracts_from_chain(raw_chain, underlying=symbol, open_interest=oi_map)
        except Exception as exc:
            print(f"  {symbol:5} chain fetch failed: {type(exc).__name__}: {exc}")
            continue

        focus = near_the_money(contracts, spot=spot) if spot else []
        stats = summarise_chain(focus, now=now, window=window, policy=liquidity)
        with_iv = sum(
            1 for snapshot in raw_chain.values() if snapshot.implied_volatility is not None
        )

        # Implied volatility lives on the snapshot, not the contract.
        atm = atm_implied_vol(raw_chain, underlying_price=spot) if spot else None
        iv_for_rank = atm[0] if atm else None

        ranking = (
            rank_instrument(symbol, closes=closes, implied_vol=iv_for_rank, policy=vol_policy)
            if closes
            else None
        )
        rv = realised_volatility(closes, vol_policy.tenor_window)

        inst = universe.BY_SYMBOL[symbol]
        snap = SymbolSnapshot(
            symbol=symbol,
            provisional=inst.provisional,
            close_count=len(closes),
            underlying_price=spot,
            chain_size=len(contracts),
            near_count=stats.considered,
            tradeable_near=stats.passing_ignoring_staleness,
            quoted=stats.quoted,
            with_iv=with_iv,
            with_delta=stats.with_delta,
            with_open_interest=stats.with_open_interest,
            median_spread_pct=stats.median_spread_pct,
            median_open_interest=stats.median_open_interest,
            passing_liquidity=stats.passing_liquidity,
            ranking=ranking,
        )
        snapshots.append(snap)

        verdict = ""
        ratio_text = "n/a"
        if isinstance(ranking, Skipped):
            verdict = ranking.reason.value
        elif isinstance(ranking, VolRanking):
            ratio_text = f"{ranking.vrp_ratio:.2f}"
            # The floor lives in rank_universe, not rank_instrument. Applying
            # it here is the difference between a report that says what would
            # trade and one that marks a 0.77 ratio as a candidate.
            liquid_enough = snap.liquid_fraction >= MIN_TRADEABLE_FRACTION
            if not liquid_enough:
                # Ranked, but the ratio is not trustworthy enough to act on.
                verdict = (
                    f"untradeable ({snap.liquid_fraction * 100:.0f}% of strikes; ratio unreliable)"
                )
            elif ranking.vrp_ratio >= vol_policy.min_vrp_ratio:
                verdict = "CANDIDATE"
                candidates += 1
            else:
                verdict = f"below floor {vol_policy.min_vrp_ratio:.2f}"
            if ranking.realised_is_expanding:
                verdict += " (vol expanding)"

        print(
            f"  {symbol:5} {'*' if snap.provisional else ' '} "
            f"{snap.close_count:>6} {snap.chain_size:>6} "
            f"{snap.iv_coverage * 100:>6.0f}% "
            f"{_fmt_pct(snap.median_spread_pct)} "
            f"{_fmt_num(snap.median_open_interest)} "
            f"{snap.liquid_fraction * 100:>6.0f}% "
            f"{_fmt_pct(rv * 100 if rv else None)} "
            f"{_fmt_pct(iv_for_rank * 100 if iv_for_rank else None)} "
            f"{ratio_text:>6}  {verdict}"
        )

    # ---- 3. Provisional instruments: promote or drop ----
    provisional = [s for s in snapshots if s.provisional]
    if provisional:
        print(f"\n{'=' * 78}\nPROVISIONAL INSTRUMENTS")
        print("=" * 78)
        for snap in provisional:
            liquid_ok = snap.liquid_fraction >= 0.25 and snap.chain_size >= 20
            spread_ok = (
                snap.median_spread_pct is not None
                and snap.median_spread_pct <= liquidity.max_spread_pct_of_mid
            )
            call = "PROMOTE" if (liquid_ok and spread_ok) else "DROP"
            print(
                f"  {snap.symbol:5} chain {snap.chain_size:>4}  "
                f"median spread {_fmt_pct(snap.median_spread_pct)}  "
                f"liquid {snap.liquid_fraction * 100:>3.0f}%  ->  {call}"
            )

    print(f"\n{'=' * 78}")
    print(f"  {candidates} of {len(snapshots)} instruments would be candidates right now.")
    if candidates == 0:
        print("  WARNING: nothing clears the premium floor. Either implied volatility")
        print("  is genuinely low across the board, or the floor is set too high.")
    print("=" * 78 + "\n")
    return 0


def main() -> int:
    return run(Settings())


if __name__ == "__main__":
    raise SystemExit(main())
