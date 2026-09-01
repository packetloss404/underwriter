"""Volatility measurement and the premium ranking that drives entries.

The strategy sells options when implied volatility is rich relative to what
the underlying is actually doing. This module produces that comparison and
nothing else -- it ranks, it does not decide. Deciding is the risk engine's
job.

Two rules carried through from the rest of the codebase:

- **Never estimate a missing input.** The Basic plan omits implied volatility
  whenever a bid or ask is zero, the underlying SIP price is unavailable, or
  the solver fails. A missing IV skips the instrument with a recorded reason.
  A fabricated IV would produce a confident ranking built on nothing.
- **Reject loudly.** Every instrument that does not become a candidate carries
  a displayable reason.
"""

from __future__ import annotations

import itertools
import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

TRADING_DAYS_PER_YEAR = 252

# Below this many returns the sample standard deviation is not meaningful
# enough to divide by.
MIN_RETURNS_FOR_VOL = 8


class Skip(StrEnum):
    """Why an instrument produced no ranking. Displayed verbatim."""

    INSUFFICIENT_HISTORY = "insufficient_history"
    NON_POSITIVE_PRICE = "non_positive_price"
    REALISED_VOL_ZERO = "realised_vol_zero"
    IMPLIED_VOL_MISSING = "implied_vol_missing"
    IMPLIED_VOL_INVALID = "implied_vol_invalid"
    PREMIUM_BELOW_FLOOR = "premium_below_floor"


@dataclass(frozen=True, slots=True)
class VolPolicy:
    # The ranking window must match the tenor we sell. We write 5-14 day
    # options, so the honest question is what volatility will be over roughly
    # the next ten sessions -- and trailing 10-session realised vol is the best
    # cheap proxy for that. A 20-session window is stale by construction: in a
    # calming market it still carries the memory of a rougher stretch weeks
    # ago, so implied vol correctly pricing the calm ahead reads as "no
    # premium" when the premium is real. Measured on SPY at kickoff: against
    # RV20 the ratio was 0.83 and the instrument was rejected; against RV10 it
    # was 1.21 and comfortably a candidate.
    tenor_window: int = 10
    # A slower window, used only to judge whether volatility is expanding.
    context_window: int = 20
    # Entry floor. Implied must exceed realised by this multiple. A hypothesis
    # to test, not a constant to tune on the judged window.
    min_vrp_ratio: float = 1.15
    # How much the short realised-vol window must exceed the long one before
    # we call volatility "expanding". Two estimates over different sample
    # sizes differ by a few percent on a perfectly stable series, so a bare
    # comparison would flip on noise -- and this gate blocks trading, so a
    # coin flip is the worst possible behaviour.
    expansion_margin: float = 0.15
    # Sanity bounds. An implied vol outside these is a data error, not a trade.
    min_plausible_iv: float = 0.01
    max_plausible_iv: float = 3.00

    @property
    def min_variance_ratio(self) -> float:
        """The live volatility-ratio floor expressed in variance units.

        This is diagnostic only.  The live gate remains ``min_vrp_ratio``;
        exposing its square makes the effective requirement explicit.  For
        example, a 1.15 volatility ratio is a 1.3225 variance ratio.
        """
        return self.min_vrp_ratio**2


def log_returns(closes: Sequence[float]) -> list[float]:
    """Close-to-close log returns.

    Raises on a non-positive price rather than returning a silently wrong
    series -- a zero or negative close is bad data, and log() of it would
    either throw deep inside a statistics call or poison the result.
    """
    if any(c <= 0 for c in closes):
        msg = "closes must all be positive"
        raise ValueError(msg)
    return [math.log(b / a) for a, b in itertools.pairwise(closes)]


def realised_volatility(closes: Sequence[float], window: int) -> float | None:
    """Annualised close-to-close realised volatility over the last `window` bars.

    Returns None when there is not enough history to be meaningful, rather than
    a small-sample number that would look authoritative.
    """
    if window < 2 or len(closes) < window + 1:
        return None
    recent = list(closes)[-(window + 1) :]
    if any(c <= 0 for c in recent):
        return None
    returns = log_returns(recent)
    if len(returns) < MIN_RETURNS_FOR_VOL:
        return None
    return statistics.stdev(returns) * math.sqrt(TRADING_DAYS_PER_YEAR)


@dataclass(frozen=True, slots=True)
class VolRanking:
    """One instrument's volatility picture."""

    symbol: str
    implied_vol: float
    # Realised volatility over a window matched to the option tenor. This is
    # the ranking denominator.
    realised_vol: float
    # Realised volatility over the slower context window, used only to detect
    # expansion. None when there is not enough history for it.
    realised_vol_context: float | None
    expansion_margin: float = 0.15

    @property
    def volatility_ratio(self) -> float:
        """Implied volatility divided by realised volatility.

        A ratio rather than a difference because the universe is
        heterogeneous: TLT and SMH sit at very different absolute volatility
        levels, so a spread measured in vol points is not comparable across
        them.

        The denominator is realised volatility over a window matched to the
        tenor we sell, not a longer one. Comparing forward-looking implied vol
        against a stale trailing window understates the premium in exactly the
        calm markets where it is most collectable.
        """
        return self.implied_vol / self.realised_vol

    @property
    def vrp_ratio(self) -> float:
        """Backward-compatible name for :attr:`volatility_ratio`.

        The strategy historically called this the ``vrp_ratio``.  Keeping that
        field preserves the live gate and existing journal consumers, while
        the explicit name prevents readers from confusing a ratio of
        volatilities with the variance risk premium used in the literature.
        """
        return self.volatility_ratio

    @property
    def vrp_points(self) -> float:
        """The premium in volatility points. Reported, not ranked on."""
        return self.implied_vol - self.realised_vol

    @property
    def implied_variance(self) -> float:
        """Annualised implied variance, in decimal-squared units."""
        return self.implied_vol**2

    @property
    def realised_variance(self) -> float:
        """Annualised realised variance, in decimal-squared units."""
        return self.realised_vol**2

    @property
    def variance_risk_premium(self) -> float:
        """Implied variance minus realised variance.

        This is the conventional ex-post variance-premium diagnostic.  It is
        deliberately not the live eligibility gate: the realised input is a
        trailing tenor-matched proxy, not a forecast of future variance.
        """
        return self.implied_variance - self.realised_variance

    @property
    def variance_ratio(self) -> float:
        """Implied variance divided by realised variance."""
        return self.implied_variance / self.realised_variance

    @property
    def relative_variance_premium(self) -> float:
        """Variance premium relative to realised variance.

        A value of 0.3225 means implied variance is 32.25% above realised
        variance.  It is useful for reading the economic meaning of the live
        volatility-ratio threshold without changing that threshold.
        """
        return self.variance_ratio - 1.0

    @property
    def realised_is_expanding(self) -> bool:
        """Whether short-window realised vol meaningfully exceeds the long one.

        The tenor window is compared against the slower context window.
        Realised volatility running hotter recently than it has over the longer
        run means the market is moving more than it was, and selling premium
        into that is selling insurance as the storm arrives.

        The margin matters. Sample standard deviations over a 10-bar and a
        20-bar window differ by a few percent even on a stable series, so a
        bare `>` would flag expansion at random. Requiring a real margin means
        this fires on signal rather than on sampling noise.
        """
        if self.realised_vol_context is None or self.realised_vol_context <= 0:
            return False
        return self.realised_vol > self.realised_vol_context * (1 + self.expansion_margin)


@dataclass(frozen=True, slots=True)
class Skipped:
    symbol: str
    reason: Skip
    detail: str = ""
    # The ranking this instrument produced before it was skipped, where one
    # exists. A below-floor instrument was measured perfectly well -- it simply
    # was not rich enough -- and discarding the figures would leave the numbers
    # recoverable only by parsing them back out of `detail`, which is prose and
    # not a format anyone maintains.
    ranking: VolRanking | None = None


def rank_instrument(
    symbol: str,
    *,
    closes: Sequence[float],
    implied_vol: float | None,
    policy: VolPolicy,
) -> VolRanking | Skipped:
    """Build one instrument's ranking, or record why it could not be built."""
    if implied_vol is None:
        return Skipped(
            symbol,
            Skip.IMPLIED_VOL_MISSING,
            "No implied volatility in the chain snapshot; not estimated.",
        )
    if not math.isfinite(implied_vol) or not (
        policy.min_plausible_iv <= implied_vol <= policy.max_plausible_iv
    ):
        return Skipped(
            symbol,
            Skip.IMPLIED_VOL_INVALID,
            f"Implied vol {implied_vol!r} outside the plausible range "
            f"[{policy.min_plausible_iv}, {policy.max_plausible_iv}].",
        )

    if any(c <= 0 for c in closes):
        return Skipped(symbol, Skip.NON_POSITIVE_PRICE, "Bar series contains a non-positive close.")

    realised = realised_volatility(closes, policy.tenor_window)
    if realised is None:
        return Skipped(
            symbol,
            Skip.INSUFFICIENT_HISTORY,
            f"Need {policy.tenor_window + 1} closes, have {len(closes)}.",
        )
    if realised <= 0:
        # A perfectly flat series. Dividing by it would report infinite premium.
        return Skipped(
            symbol,
            Skip.REALISED_VOL_ZERO,
            "Realised volatility is zero; the premium ratio is undefined.",
        )

    return VolRanking(
        symbol=symbol,
        implied_vol=implied_vol,
        realised_vol=realised,
        realised_vol_context=realised_volatility(closes, policy.context_window),
        expansion_margin=policy.expansion_margin,
    )


def rank_universe(
    inputs: Mapping[str, tuple[Sequence[float], float | None]],
    *,
    policy: VolPolicy | None = None,
) -> tuple[list[VolRanking], list[Skipped]]:
    """Rank every instrument, richest premium first.

    `inputs` maps symbol to (closes, implied_vol). Returns candidates that meet
    the premium floor, plus everything skipped and why -- including instruments
    that ranked fine but sat below the floor, since "we looked and it was not
    rich enough" is a decision worth displaying.
    """
    policy = policy or VolPolicy()
    ranked: list[VolRanking] = []
    skipped: list[Skipped] = []

    for symbol, (closes, implied_vol) in inputs.items():
        result = rank_instrument(symbol, closes=closes, implied_vol=implied_vol, policy=policy)
        if isinstance(result, Skipped):
            skipped.append(result)
        elif result.vrp_ratio < policy.min_vrp_ratio:
            skipped.append(
                Skipped(
                    symbol,
                    Skip.PREMIUM_BELOW_FLOOR,
                    f"Premium ratio {result.vrp_ratio:.2f} is below the "
                    f"{policy.min_vrp_ratio:.2f} floor "
                    f"(IV {result.implied_vol:.1%} vs RV {result.realised_vol:.1%}).",
                    ranking=result,
                )
            )
        else:
            ranked.append(result)

    ranked.sort(key=lambda r: r.vrp_ratio, reverse=True)
    return ranked, skipped
