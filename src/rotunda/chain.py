"""Option contract selection and vertical-spread construction.

Two responsibilities, both of which have to be defensible in an audit log:

1. Ask for the right slice of the chain. `GET /v2/options/contracts` does not
   error when expiry bounds are omitted -- it quietly returns only contracts
   expiring before the coming weekend. That presents as a liquidity problem
   rather than a missing parameter, so this module refuses to build a request
   without explicit bounds. See docs/GOTCHAS.md #1.
2. Reject loudly. Every candidate that does not become a trade carries a reason
   the dashboard can display. Silent filtering is how a strategy becomes
   unexplainable.

Greeks are optional throughout. The Basic plan omits delta whenever a bid or
ask is zero, the underlying SIP price is unavailable, the contract expires that
day, or the IV solver fails. Selection falls back to deterministic moneyness
rules in that case. It never fabricates a Greek.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum


class ContractType(StrEnum):
    CALL = "call"
    PUT = "put"


class Rejection(StrEnum):
    """Why a contract or spread was not traded. Displayed verbatim."""

    NO_QUOTE = "no_quote"
    ZERO_BID = "zero_bid"
    CROSSED_QUOTE = "crossed_quote"
    STALE_QUOTE = "stale_quote"
    SPREAD_TOO_WIDE = "spread_too_wide"
    OPEN_INTEREST_TOO_LOW = "open_interest_too_low"
    OUTSIDE_EXPIRY_WINDOW = "outside_expiry_window"
    WRONG_TYPE = "wrong_type"
    NO_LONG_LEG_CANDIDATE = "no_long_leg_candidate"
    NO_SHORT_LEG_CANDIDATE = "no_short_leg_candidate"
    NO_VIABLE_WIDTH = "no_viable_width"
    DEBIT_EXCEEDS_WIDTH = "debit_exceeds_width"
    REWARD_RISK_TOO_LOW = "reward_risk_too_low"
    NO_UNDERLYING_PRICE = "no_underlying_price"
    EXCEEDS_RISK_BUDGET = "exceeds_risk_budget"


@dataclass(frozen=True, slots=True)
class Quote:
    bid: float
    ask: float
    as_of: datetime

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def width(self) -> float:
        return self.ask - self.bid

    @property
    def width_pct_of_mid(self) -> float:
        """Relative spread. Infinite at a zero mid so it always fails a filter."""
        return float("inf") if self.mid <= 0 else (self.width / self.mid) * 100


@dataclass(frozen=True, slots=True)
class Contract:
    symbol: str
    underlying: str
    expiry: date
    strike: float
    contract_type: ContractType
    quote: Quote | None = None
    delta: float | None = None
    open_interest: int | None = None

    def days_to_expiry(self, as_of: date) -> int:
        return (self.expiry - as_of).days


@dataclass(frozen=True, slots=True)
class Rejected:
    contract: Contract
    reason: Rejection
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ExpiryWindow:
    """An explicit, inclusive expiry range.

    Constructing this is the only supported way to bound a contract query, so
    the truncation gotcha cannot recur by omission.
    """

    gte: date
    lte: date

    def __post_init__(self) -> None:
        if self.gte > self.lte:
            msg = f"expiry window is inverted: gte={self.gte} > lte={self.lte}"
            raise ValueError(msg)

    @classmethod
    def from_dte(cls, as_of: date, min_days: int, max_days: int) -> ExpiryWindow:
        if min_days > max_days:
            msg = f"min_days ({min_days}) exceeds max_days ({max_days})"
            raise ValueError(msg)
        if min_days < 1:
            # A 0DTE contract has no Greeks under the Basic plan and pins into
            # expiry the same session. The strategy forbids it outright.
            msg = f"min_days must be at least 1 to exclude 0DTE, got {min_days}"
            raise ValueError(msg)
        return cls(gte=as_of + timedelta(days=min_days), lte=as_of + timedelta(days=max_days))

    def contains(self, expiry: date) -> bool:
        return self.gte <= expiry <= self.lte

    def as_query_params(self) -> dict[str, str]:
        """Both bounds, always. Never emit one without the other."""
        return {
            "expiration_date_gte": self.gte.isoformat(),
            "expiration_date_lte": self.lte.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class LiquidityPolicy:
    max_spread_pct_of_mid: float = 10.0
    min_open_interest: int = 100
    max_quote_age_seconds: float = 30.0
    # Open interest is absent often enough on the Basic plan that treating
    # "missing" as "fail" would empty the chain. Missing is tolerated and
    # recorded; an explicit low value is not.
    require_open_interest: bool = False


def screen_contract(
    contract: Contract,
    *,
    now: datetime,
    window: ExpiryWindow,
    wanted: ContractType,
    policy: LiquidityPolicy,
) -> Rejection | None:
    """Return the reason this contract is untradeable, or None if it passes."""
    if contract.contract_type is not wanted:
        return Rejection.WRONG_TYPE
    if not window.contains(contract.expiry):
        return Rejection.OUTSIDE_EXPIRY_WINDOW

    quote = contract.quote
    if quote is None:
        return Rejection.NO_QUOTE
    if quote.bid <= 0:
        # A zero bid means nothing is willing to buy it. We could never exit.
        return Rejection.ZERO_BID
    if quote.ask < quote.bid:
        return Rejection.CROSSED_QUOTE
    if (now - quote.as_of).total_seconds() > policy.max_quote_age_seconds:
        return Rejection.STALE_QUOTE
    if quote.width_pct_of_mid > policy.max_spread_pct_of_mid:
        return Rejection.SPREAD_TOO_WIDE

    oi = contract.open_interest
    if oi is None:
        if policy.require_open_interest:
            return Rejection.OPEN_INTEREST_TOO_LOW
    elif oi < policy.min_open_interest:
        return Rejection.OPEN_INTEREST_TOO_LOW

    return None


def screen(
    contracts: Iterable[Contract],
    *,
    now: datetime,
    window: ExpiryWindow,
    wanted: ContractType,
    policy: LiquidityPolicy,
) -> tuple[list[Contract], list[Rejected]]:
    """Partition a chain into tradeable contracts and recorded rejections."""
    passed: list[Contract] = []
    rejected: list[Rejected] = []
    for contract in contracts:
        reason = screen_contract(contract, now=now, window=window, wanted=wanted, policy=policy)
        if reason is None:
            passed.append(contract)
        else:
            rejected.append(Rejected(contract=contract, reason=reason))
    return passed, rejected


@dataclass(frozen=True, slots=True)
class DeltaPolicy:
    """Target absolute-delta bands for each leg.

    Bands, not points, because the Basic plan's chain is sparse and an exact
    delta target would reject constantly. `moneyness_fallback_pct` is used when
    delta is absent: the long leg is chosen near-the-money and the short leg a
    fixed percentage out, which approximates the delta bands without inventing
    a Greek.
    """

    long_leg_min: float = 0.55
    long_leg_max: float = 0.70
    short_leg_min: float = 0.20
    short_leg_max: float = 0.35
    long_moneyness_fallback_pct: float = 1.0
    short_moneyness_fallback_pct: float = 4.0


@dataclass(frozen=True, slots=True)
class SpreadEconomics:
    min_reward_risk: float = 0.45
    # Assume we cross a fraction of each leg's quoted spread. Paper fills are
    # simulated against modified indicative quotes and the multi-leg fill model
    # is undocumented, so the honest number assumes we pay to get in.
    # See docs/GOTCHAS.md #3.
    slippage_fraction_of_spread: float = 0.5


@dataclass(frozen=True, slots=True)
class VerticalSpread:
    """A defined-risk debit vertical, priced conservatively.

    All figures are per-contract and in dollars, so a $1-wide SPY spread has
    `width = 1.0` and `max_loss = debit * 100`.
    """

    long_leg: Contract
    short_leg: Contract
    debit: float
    contract_type: ContractType

    @property
    def underlying(self) -> str:
        return self.long_leg.underlying

    @property
    def expiry(self) -> date:
        return self.long_leg.expiry

    @property
    def width(self) -> float:
        return abs(self.short_leg.strike - self.long_leg.strike)

    @property
    def max_loss(self) -> float:
        """The full debit paid. This is the defined risk."""
        return self.debit * 100

    @property
    def max_profit(self) -> float:
        return (self.width - self.debit) * 100

    @property
    def reward_risk(self) -> float:
        return float("inf") if self.max_loss <= 0 else self.max_profit / self.max_loss


def _conservative_debit(
    long_leg: Contract, short_leg: Contract, economics: SpreadEconomics
) -> float | None:
    """Price the spread assuming we cross part of both quoted spreads.

    Buying the long leg pays up from mid; selling the short leg receives less
    than mid. Returns None when either leg lacks a quote.
    """
    if long_leg.quote is None or short_leg.quote is None:
        return None
    slip = economics.slippage_fraction_of_spread
    pay = long_leg.quote.mid + long_leg.quote.width * slip
    receive = short_leg.quote.mid - short_leg.quote.width * slip
    return pay - receive


def _pick_by_delta(candidates: Sequence[Contract], low: float, high: float) -> list[Contract]:
    """Contracts inside the delta band, ordered by closeness to its centre.

    Order matters: the pair chooser prefers earlier entries, which keeps
    selection near the middle of the band the strategy spec asked for rather
    than drifting to whichever edge happens to price best.
    """
    centre = (low + high) / 2
    inside = [c for c in candidates if c.delta is not None and low <= abs(c.delta) <= high]
    return sorted(inside, key=lambda c: abs(abs(c.delta or 0.0) - centre))


def _pick_by_moneyness(
    candidates: Sequence[Contract],
    *,
    underlying_price: float,
    target_pct_otm: float,
    contract_type: ContractType,
) -> list[Contract]:
    """Deterministic fallback when Greeks are missing.

    A call `target_pct_otm` above spot, or a put the same distance below,
    stands in for the delta band. Sorted by distance from that target so the
    caller can take the closest.
    """
    if contract_type is ContractType.CALL:
        target = underlying_price * (1 + target_pct_otm / 100)
    else:
        target = underlying_price * (1 - target_pct_otm / 100)
    return sorted(candidates, key=lambda c: abs(c.strike - target))


def select_vertical(
    contracts: Iterable[Contract],
    *,
    now: datetime,
    window: ExpiryWindow,
    contract_type: ContractType,
    underlying_price: float | None,
    policy: LiquidityPolicy | None = None,
    deltas: DeltaPolicy | None = None,
    economics: SpreadEconomics | None = None,
) -> tuple[VerticalSpread | None, Rejection | None, list[Rejected]]:
    """Build the best debit vertical from a chain.

    Returns `(spread, rejection, screened_out)`. Exactly one of `spread` and
    `rejection` is set. `screened_out` always carries the per-contract
    rejections so the dashboard can show what was considered and discarded.
    """
    policy = policy or LiquidityPolicy()
    deltas = deltas or DeltaPolicy()
    economics = economics or SpreadEconomics()

    tradeable, screened_out = screen(
        contracts, now=now, window=window, wanted=contract_type, policy=policy
    )
    if not tradeable:
        return None, Rejection.NO_LONG_LEG_CANDIDATE, screened_out

    if any(c.delta is not None for c in tradeable):
        longs = _pick_by_delta(tradeable, deltas.long_leg_min, deltas.long_leg_max)
        shorts = _pick_by_delta(tradeable, deltas.short_leg_min, deltas.short_leg_max)
    elif underlying_price is None:
        # No delta and no spot means no defensible way to choose strikes.
        return None, Rejection.NO_UNDERLYING_PRICE, screened_out
    else:
        longs = _pick_by_moneyness(
            tradeable,
            underlying_price=underlying_price,
            target_pct_otm=-deltas.long_moneyness_fallback_pct,
            contract_type=contract_type,
        )
        shorts = _pick_by_moneyness(
            tradeable,
            underlying_price=underlying_price,
            target_pct_otm=deltas.short_moneyness_fallback_pct,
            contract_type=contract_type,
        )

    if not longs:
        return None, Rejection.NO_LONG_LEG_CANDIDATE, screened_out
    if not shorts:
        return None, Rejection.NO_SHORT_LEG_CANDIDATE, screened_out

    # Candidates are ordered best-first, so a pair's combined index measures how
    # far it sits from the structure the spec asked for. Selecting on that
    # rather than on maximum reward:risk matters: reward:risk improves
    # monotonically as the short leg moves further out, so maximising it would
    # reliably pick the widest, cheapest, least probable spread on the board.
    # The minimum reward:risk stays a floor; it is not the objective.
    long_rank = {c.symbol: i for i, c in enumerate(longs)}
    short_rank = {c.symbol: i for i, c in enumerate(shorts)}

    best: VerticalSpread | None = None
    best_score: tuple[int, float] | None = None
    saw_viable_width = False
    saw_priceable_pair = False

    for long_leg in longs:
        for short_leg in shorts:
            # A debit vertical needs the short leg further OTM, same expiry.
            if long_leg.expiry != short_leg.expiry:
                continue
            if contract_type is ContractType.CALL:
                if short_leg.strike <= long_leg.strike:
                    continue
            elif short_leg.strike >= long_leg.strike:
                continue
            saw_viable_width = True

            debit = _conservative_debit(long_leg, short_leg, economics)
            if debit is None or debit <= 0:
                continue
            saw_priceable_pair = True

            candidate = VerticalSpread(
                long_leg=long_leg,
                short_leg=short_leg,
                debit=debit,
                contract_type=contract_type,
            )
            # A debit at or above the width has no profit left in it.
            if candidate.debit >= candidate.width:
                continue
            if candidate.reward_risk < economics.min_reward_risk:
                continue
            # Lower rank sum is closer to the target structure; ties break on
            # the better payoff.
            score = (
                long_rank[long_leg.symbol] + short_rank[short_leg.symbol],
                -candidate.reward_risk,
            )
            if best_score is None or score < best_score:
                best, best_score = candidate, score

    if best is not None:
        return best, None, screened_out
    if not saw_viable_width:
        return None, Rejection.NO_VIABLE_WIDTH, screened_out
    if not saw_priceable_pair:
        return None, Rejection.DEBIT_EXCEEDS_WIDTH, screened_out
    return None, Rejection.REWARD_RISK_TOO_LOW, screened_out


# --------------------------------------------------------------------------
# Credit verticals -- the structure the volatility strategy actually trades.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreditPolicy:
    """Targets for a short premium vertical.

    The short leg sits out of the money at a delta where the option is more
    likely than not to expire worthless; the long leg caps the loss.
    """

    short_leg_min_delta: float = 0.15
    short_leg_max_delta: float = 0.30
    # Credit must be a meaningful fraction of the width, otherwise we are
    # taking the full width of risk to collect almost nothing.
    min_credit_fraction_of_width: float = 0.15
    max_credit_fraction_of_width: float = 0.50
    # Strikes between the legs.
    #
    # Width is NOT a lever on expected return. Max loss scales with width, so a
    # fixed dollar risk budget buys proportionally fewer contracts and the
    # total credit collected is roughly unchanged. What actually governs return
    # per unit of risk is credit as a fraction of width, which the selector
    # optimises directly. Width governs sizing granularity: narrower spreads
    # divide the risk budget more finely.
    min_width: float = 1.0
    max_width: float = 10.0
    short_moneyness_fallback_pct: float = 3.0
    # Hard ceiling on per-contract risk, in dollars. Without it the selector
    # happily returns a structure whose max loss exceeds the per-trade budget;
    # sizing then floors to zero contracts and the agent stops trading with no
    # stated reason. Refusing here produces a displayable rejection instead.
    max_loss_per_contract: float | None = None


@dataclass(frozen=True, slots=True)
class CreditSpread:
    """A defined-risk short-premium vertical.

    Economics are the mirror of a debit spread: the credit received is the
    maximum profit, and the maximum loss is what remains of the width.
    """

    short_leg: Contract
    long_leg: Contract
    credit: float
    contract_type: ContractType

    @property
    def underlying(self) -> str:
        return self.short_leg.underlying

    @property
    def expiry(self) -> date:
        return self.short_leg.expiry

    @property
    def width(self) -> float:
        return abs(self.short_leg.strike - self.long_leg.strike)

    @property
    def max_profit(self) -> float:
        """The credit received, kept if both legs expire worthless."""
        return self.credit * 100

    @property
    def max_loss(self) -> float:
        """Width minus credit. Bounded, and known before entry."""
        return (self.width - self.credit) * 100

    @property
    def reward_risk(self) -> float:
        return float("inf") if self.max_loss <= 0 else self.max_profit / self.max_loss

    @property
    def credit_fraction_of_width(self) -> float:
        return 0.0 if self.width <= 0 else self.credit / self.width


def _conservative_credit(
    short_leg: Contract, long_leg: Contract, economics: SpreadEconomics
) -> float | None:
    """Price the credit assuming we cross part of both quoted spreads.

    Selling the short leg receives less than mid; buying the long leg pays more
    than mid. Both move against us, so the modelled credit is below the naive
    mid-to-mid figure.
    """
    if short_leg.quote is None or long_leg.quote is None:
        return None
    slip = economics.slippage_fraction_of_spread
    receive = short_leg.quote.mid - short_leg.quote.width * slip
    pay = long_leg.quote.mid + long_leg.quote.width * slip
    return receive - pay


def select_credit_vertical(
    contracts: Iterable[Contract],
    *,
    now: datetime,
    window: ExpiryWindow,
    contract_type: ContractType,
    underlying_price: float | None,
    policy: LiquidityPolicy | None = None,
    credit_policy: CreditPolicy | None = None,
    economics: SpreadEconomics | None = None,
) -> tuple[CreditSpread | None, Rejection | None, list[Rejected]]:
    """Build the best short-premium vertical from a chain.

    Returns `(spread, rejection, screened_out)`; exactly one of `spread` and
    `rejection` is set. As with debit selection, the objective is closeness to
    the target structure rather than maximum credit -- collecting the most
    premium always means selling the strike nearest the money, which is also
    the one most likely to be breached.
    """
    policy = policy or LiquidityPolicy()
    credit_policy = credit_policy or CreditPolicy()
    economics = economics or SpreadEconomics()

    tradeable, screened_out = screen(
        contracts, now=now, window=window, wanted=contract_type, policy=policy
    )
    if not tradeable:
        return None, Rejection.NO_SHORT_LEG_CANDIDATE, screened_out

    if any(c.delta is not None for c in tradeable):
        shorts = _pick_by_delta(
            tradeable, credit_policy.short_leg_min_delta, credit_policy.short_leg_max_delta
        )
    elif underlying_price is None:
        return None, Rejection.NO_UNDERLYING_PRICE, screened_out
    else:
        shorts = _pick_by_moneyness(
            tradeable,
            underlying_price=underlying_price,
            target_pct_otm=credit_policy.short_moneyness_fallback_pct,
            contract_type=contract_type,
        )

    if not shorts:
        return None, Rejection.NO_SHORT_LEG_CANDIDATE, screened_out

    short_rank = {c.symbol: i for i, c in enumerate(shorts)}
    best: CreditSpread | None = None
    best_score: tuple[int, float] | None = None
    saw_viable_width = False
    saw_priceable_pair = False
    saw_unaffordable = False

    for short_leg in shorts:
        for long_leg in tradeable:
            if long_leg.expiry != short_leg.expiry:
                continue
            # The long leg is further out of the money than the short leg:
            # below it for puts, above it for calls.
            if contract_type is ContractType.PUT:
                if long_leg.strike >= short_leg.strike:
                    continue
            elif long_leg.strike <= short_leg.strike:
                continue

            width = abs(short_leg.strike - long_leg.strike)
            if not (credit_policy.min_width <= width <= credit_policy.max_width):
                continue
            saw_viable_width = True

            credit = _conservative_credit(short_leg, long_leg, economics)
            if credit is None or credit <= 0:
                continue
            saw_priceable_pair = True

            candidate = CreditSpread(
                short_leg=short_leg,
                long_leg=long_leg,
                credit=credit,
                contract_type=contract_type,
            )
            fraction = candidate.credit_fraction_of_width
            # Too little credit means taking the width of risk for nothing.
            # Too much means the short strike is effectively at the money and
            # the "high probability" premise is false.
            if not (
                credit_policy.min_credit_fraction_of_width
                <= fraction
                <= credit_policy.max_credit_fraction_of_width
            ):
                continue

            # A structure we cannot afford is not a candidate. Letting it
            # through would size to zero contracts and stop trading silently.
            if (
                credit_policy.max_loss_per_contract is not None
                and candidate.max_loss > credit_policy.max_loss_per_contract
            ):
                saw_unaffordable = True
                continue

            score = (short_rank[short_leg.symbol], -candidate.credit_fraction_of_width)
            if best_score is None or score < best_score:
                best, best_score = candidate, score

    if best is not None:
        return best, None, screened_out
    if not saw_viable_width:
        return None, Rejection.NO_VIABLE_WIDTH, screened_out
    if not saw_priceable_pair:
        return None, Rejection.DEBIT_EXCEEDS_WIDTH, screened_out
    if saw_unaffordable:
        return None, Rejection.EXCEEDS_RISK_BUDGET, screened_out
    return None, Rejection.REWARD_RISK_TOO_LOW, screened_out
