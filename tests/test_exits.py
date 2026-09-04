"""Exit tests.

An agent that cannot close a position it opened is worse than one that never
opened it, so these weight toward the triggers firing when they must and the
ordering holding when several fire at once.

None of the exit logic consults the model. Risk management that waits on an API
call is not risk management, and there is a test asserting the module imports
nothing that could.
"""

from __future__ import annotations

from datetime import date, time

import pytest

from underwriter.config import RiskLimits
from underwriter.exits import (
    EXIT_FORCING_BLOCKS,
    ExitDecision,
    ExitPolicy,
    ExitReason,
    closing_debit,
    decide_exit,
    decide_exits,
    realised_if_closed,
)
from underwriter.positions import OpenSpread
from underwriter.regime import Blocked, RegimeBlock, RegimeVerdict

SHORT = "SPY260911P00760000"
LONG = "SPY260911P00755000"
EXPIRY = date(2026, 9, 11)
MIDDAY = time(11, 0)
CALM = RegimeVerdict()


def spread(
    *,
    underlying: str = "SPY",
    credit: float = 0.42,
    spreads: float = 2.0,
    expiry: date = EXPIRY,
    cid: str = "uw-A",
) -> OpenSpread:
    return OpenSpread(
        underlying=underlying,
        short_symbol=SHORT,
        long_symbol=LONG,
        expiry=expiry,
        spreads=spreads,
        width=5.0,
        credit_per_spread=credit,
        max_loss=(5.0 - credit) * 100 * spreads,
        net_delta=14.0,
        unrealised_pnl=0.0,
        client_order_id=cid,
    )


def decide(
    *,
    quotes: dict[str, float | None] | None = None,
    regime: RegimeVerdict = CALM,
    today: date = date(2026, 8, 31),
    now_et: time = MIDDAY,
    position: OpenSpread | None = None,
    policy: ExitPolicy | None = None,
) -> ExitDecision:
    return decide_exit(
        position or spread(),
        quotes=quotes if quotes is not None else {SHORT: 0.40, LONG: 0.10},
        regime=regime,
        today=today,
        now_et=now_et,
        limits=RiskLimits(),
        policy=policy,
    )


class TestClosingPrice:
    def test_debit_is_short_minus_long(self) -> None:
        assert closing_debit(spread(), {SHORT: 0.40, LONG: 0.10}) == pytest.approx(0.30)

    @pytest.mark.parametrize("quotes", [{}, {SHORT: 0.40}, {LONG: 0.10}])
    def test_a_missing_leg_yields_none_not_zero(self, quotes: dict[str, float | None]) -> None:
        # A missing quote is not a free exit.
        assert closing_debit(spread(), quotes) is None

    def test_realised_is_a_position_total(self) -> None:
        # Collected 0.42, close at 0.30, two spreads: 0.12 x 100 x 2.
        assert realised_if_closed(spread(), 0.30) == pytest.approx(24.0)

    def test_realised_is_negative_on_a_loser(self) -> None:
        assert realised_if_closed(spread(), 1.00) < 0


class TestTriggers:
    def test_healthy_position_holds(self) -> None:
        d = decide()
        assert not d.should_exit
        assert "Costs 0.30" in d.detail

    def test_profit_target_fires(self) -> None:
        d = decide(quotes={SHORT: 0.18, LONG: 0.05})
        assert d.reason is ExitReason.PROFIT_TARGET
        # The only non-urgent exit: it is a preference, not a deadline.
        assert not d.urgent

    def test_loss_limit_fires(self) -> None:
        d = decide(quotes={SHORT: 1.30, LONG: 0.35})
        assert d.reason is ExitReason.LOSS_LIMIT
        assert d.urgent

    def test_time_stop_fires_inside_the_floor(self) -> None:
        d = decide(today=date(2026, 9, 9))
        assert d.reason is ExitReason.TIME_STOP
        assert d.urgent

    def test_hard_flatten_fires_late_on_expiry_day(self) -> None:
        d = decide(today=EXPIRY, now_et=time(15, 30))
        assert d.reason is ExitReason.HARD_FLATTEN
        assert d.urgent

    def test_expiry_day_before_the_cutoff_is_not_yet_a_flatten(self) -> None:
        # Still exits, but on the time stop rather than the deadline.
        d = decide(today=EXPIRY, now_et=time(10, 0))
        assert d.reason is ExitReason.TIME_STOP

    def test_a_missing_quote_holds_and_says_why(self) -> None:
        d = decide(quotes={})
        assert not d.should_exit
        assert "no quote" in d.detail


class TestRegimeBreak:
    def _verdict(self, block: RegimeBlock) -> RegimeVerdict:
        return RegimeVerdict(blocks=(Blocked(block, "detail"),))

    @pytest.mark.parametrize("block", sorted(EXIT_FORCING_BLOCKS))
    def test_exit_forcing_blocks_close_the_position(self, block: RegimeBlock) -> None:
        d = decide(regime=self._verdict(block))
        assert d.reason is ExitReason.REGIME_BREAK
        assert d.urgent

    def test_below_trend_does_not_force_an_exit(self) -> None:
        # An ordinary condition. Exiting on it would churn the book on noise,
        # and regime.py only ever blocks entries for the same reason.
        d = decide(regime=self._verdict(RegimeBlock.BENCHMARK_BELOW_TREND))
        assert not d.should_exit

    def test_a_scheduled_event_is_advisory_for_exits(self) -> None:
        d = decide(regime=self._verdict(RegimeBlock.SCHEDULED_EVENT))
        assert not d.should_exit

    def test_the_forcing_set_is_deliberately_narrow(self) -> None:
        assert {
            RegimeBlock.TERM_STRUCTURE_INVERTED,
            RegimeBlock.BENCHMARK_DRAWDOWN,
        } == EXIT_FORCING_BLOCKS


class TestPrecedence:
    """A position can satisfy several triggers. The reason recorded is the one
    that governs what we do about it."""

    def test_flatten_beats_profit_target(self) -> None:
        # The target is a preference; the window is a deadline.
        d = decide(quotes={SHORT: 0.18, LONG: 0.05}, today=EXPIRY, now_et=time(15, 30))
        assert d.reason is ExitReason.HARD_FLATTEN

    def test_flatten_beats_loss_limit(self) -> None:
        d = decide(quotes={SHORT: 1.30, LONG: 0.35}, today=EXPIRY, now_et=time(15, 30))
        assert d.reason is ExitReason.HARD_FLATTEN

    def test_time_stop_beats_loss_limit(self) -> None:
        d = decide(quotes={SHORT: 1.30, LONG: 0.35}, today=date(2026, 9, 9))
        assert d.reason is ExitReason.TIME_STOP

    def test_loss_limit_beats_regime_break(self) -> None:
        # Both urgent, but the loss is the specific fact about this position.
        d = decide(
            quotes={SHORT: 1.30, LONG: 0.35},
            regime=RegimeVerdict(blocks=(Blocked(RegimeBlock.TERM_STRUCTURE_INVERTED, "x"),)),
        )
        assert d.reason is ExitReason.LOSS_LIMIT

    def test_regime_break_beats_profit_target(self) -> None:
        d = decide(
            quotes={SHORT: 0.18, LONG: 0.05},
            regime=RegimeVerdict(blocks=(Blocked(RegimeBlock.TERM_STRUCTURE_INVERTED, "x"),)),
        )
        assert d.reason is ExitReason.REGIME_BREAK


class TestPolicy:
    def test_profit_fraction_is_configurable(self) -> None:
        greedy = ExitPolicy(profit_take_fraction=0.10)
        assert not decide(quotes={SHORT: 0.18, LONG: 0.05}, policy=greedy).should_exit

    def test_loss_multiple_is_configurable(self) -> None:
        tight = ExitPolicy(loss_multiple=1.2)
        d = decide(quotes={SHORT: 0.70, LONG: 0.15}, policy=tight)
        assert d.reason is ExitReason.LOSS_LIMIT

    def test_zero_credit_does_not_divide_by_zero(self) -> None:
        # Defensive: a position whose recorded credit is missing must not
        # crash the whole exit sweep.
        d = decide(position=spread(credit=0.0))
        assert not d.should_exit


class TestSweep:
    def test_urgent_exits_are_ordered_first(self) -> None:
        # Matters when the book is larger than the orders we can place in one
        # cycle: deadline-driven exits must go first.
        healthy = spread(underlying="XLE", cid="uw-B")
        expiring = spread(underlying="SPY", cid="uw-A", expiry=date(2026, 9, 1))
        results = decide_exits(
            [healthy, expiring],
            quotes={SHORT: 0.40, LONG: 0.10},
            regime=CALM,
            today=date(2026, 8, 31),
            now_et=MIDDAY,
            limits=RiskLimits(),
        )
        assert results[0].urgent
        assert results[0].spread.underlying == "SPY"

    def test_every_position_gets_a_decision(self) -> None:
        book = [spread(underlying=s, cid=f"uw-{s}") for s in ("SPY", "XLE", "GLD")]
        results = decide_exits(
            book,
            quotes={SHORT: 0.40, LONG: 0.10},
            regime=CALM,
            today=date(2026, 8, 31),
            now_et=MIDDAY,
            limits=RiskLimits(),
        )
        assert len(results) == len(book)

    def test_an_empty_book_is_fine(self) -> None:
        assert (
            decide_exits(
                [],
                quotes={},
                regime=CALM,
                today=date(2026, 8, 31),
                now_et=MIDDAY,
                limits=RiskLimits(),
            )
            == []
        )


class TestIndependenceFromTheModel:
    def test_exits_never_import_an_llm_client(self) -> None:
        # The property that makes exits trustworthy: they work when the model
        # is down, rate-limited, or returning nonsense.
        source = __import__("pathlib").Path("src/underwriter/exits.py").read_text()
        for forbidden in ("anthropic", "openai", "httpx", "requests"):
            assert forbidden not in source
