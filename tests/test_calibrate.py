"""Calibration tests.

Calibration exists to answer one question with real data: would these
thresholds ever fire? Its own logic therefore has to be trustworthy, so the
pure parts -- the regime replay and the chain measurement -- are tested here
without a network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from underwriter.calibrate import (
    ChainStats,
    SymbolSnapshot,
    TrendAlternative,
    evaluate_trend_shadows,
    premium_floor_shadows,
    replay_regime,
    replay_trend_shadows,
    summarise_chain,
)
from underwriter.chain import (
    Contract,
    ContractType,
    ExpiryWindow,
    LiquidityPolicy,
    Quote,
)
from underwriter.volatility import VolRanking

NOW = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)
WINDOW = ExpiryWindow.from_dte(date(2026, 8, 31), 5, 14)
EXPIRY = date(2026, 9, 11)


def contract(
    strike: float,
    *,
    bid: float = 1.00,
    ask: float = 1.10,
    delta: float | None = -0.25,
    oi: int | None = 5000,
    quote: bool = True,
    age_s: float = 2.0,
    kind: ContractType = ContractType.PUT,
) -> Contract:
    return Contract(
        symbol=f"SPY260911{'P' if kind is ContractType.PUT else 'C'}{int(strike * 1000):08d}",
        underlying="SPY",
        expiry=EXPIRY,
        strike=strike,
        contract_type=kind,
        quote=Quote(bid, ask, NOW - timedelta(seconds=age_s)) if quote else None,
        delta=delta,
        open_interest=oi,
    )


def rising(n: int = 60, start: float = 600.0, step: float = 1.0) -> list[float]:
    return [start + i * step for i in range(n)]


def falling(n: int = 60, start: float = 700.0, step: float = -1.0) -> list[float]:
    return [start + i * step for i in range(n)]


class TestRegimeReplay:
    def test_a_steady_uptrend_permits_most_sessions(self) -> None:
        history = replay_regime(rising())
        assert history.sessions > 0
        assert history.permitted_pct > 90

    def test_a_steady_downtrend_permits_almost_none(self) -> None:
        history = replay_regime(falling())
        assert history.permitted_pct < 10
        assert "benchmark_below_trend" in history.blocks

    def test_every_session_is_accounted_for(self) -> None:
        history = replay_regime(rising(60), warmup=25)
        assert history.sessions == 60 - 25 + 1

    def test_short_history_yields_no_sessions(self) -> None:
        # Fewer closes than the warmup means nothing to replay, not a crash.
        assert replay_regime(rising(10), warmup=25).sessions == 0

    def test_block_reasons_are_counted(self) -> None:
        history = replay_regime(falling())
        assert sum(history.blocks.values()) >= history.sessions - history.permitted

    def test_permitted_pct_is_zero_for_an_empty_replay(self) -> None:
        assert replay_regime([], warmup=25).permitted_pct == 0.0


class TestShadowTrendCalibration:
    def test_a_small_one_day_dip_distinguishes_the_three_hypotheses(self) -> None:
        # The latest close is 0.38% under its current 20MA.  Production's hard
        # gate blocks, while the 0.5% buffer and two-close confirmation permit.
        results = {result.rule: result for result in evaluate_trend_shadows([100.0] * 20 + [99.6])}

        assert not results[TrendAlternative.HARD_20MA].may_open
        assert results[TrendAlternative.BUFFER_0_5_PCT].may_open
        assert results[TrendAlternative.TWO_CLOSE_CONFIRMATION].may_open

    def test_two_consecutive_closes_under_their_own_averages_confirm(self) -> None:
        results = {
            result.rule: result for result in evaluate_trend_shadows([100.0] * 20 + [99.8, 99.6])
        }
        assert not results[TrendAlternative.TWO_CLOSE_CONFIRMATION].may_open

    def test_missing_history_blocks_every_shadow_rule(self) -> None:
        results = evaluate_trend_shadows([100.0] * 10)
        assert len(results) == len(TrendAlternative)
        assert all(not result.may_open for result in results)

    def test_replay_accounts_for_every_rule_on_every_session(self) -> None:
        histories = replay_trend_shadows(rising(60), warmup=21)
        assert set(histories) == set(TrendAlternative)
        assert all(history.sessions == 40 for history in histories.values())
        assert all(history.permitted_pct == 100 for history in histories.values())


def snapshot(symbol: str, ratio: float, *, liquid_fraction: float = 1.0) -> SymbolSnapshot:
    near = 10
    return SymbolSnapshot(
        symbol=symbol,
        provisional=False,
        close_count=60,
        underlying_price=100.0,
        chain_size=20,
        near_count=near,
        tradeable_near=round(near * liquid_fraction),
        quoted=20,
        with_iv=20,
        with_delta=20,
        with_open_interest=20,
        median_spread_pct=5.0,
        median_open_interest=1000.0,
        passing_liquidity=10,
        ranking=VolRanking(
            symbol=symbol,
            implied_vol=0.20 * ratio,
            realised_vol=0.20,
            realised_vol_context=0.20,
        ),
    )


class TestShadowPremiumCalibration:
    def test_counts_each_floor_without_changing_the_rankings(self) -> None:
        snapshots = [snapshot("A", 1.16), snapshot("B", 1.08), snapshot("C", 0.99)]
        shadows = premium_floor_shadows(snapshots)

        assert [(result.floor, result.candidates) for result in shadows] == [
            (1.00, 2),
            (1.05, 2),
            (1.10, 1),
            (1.15, 1),
        ]
        assert [
            snap.ranking.vrp_ratio
            for snap in snapshots
            if isinstance(snap.ranking, VolRanking)
        ] == pytest.approx([1.16, 1.08, 0.99])

    def test_an_untradeable_rich_quote_is_excluded_at_every_floor(self) -> None:
        shadows = premium_floor_shadows([snapshot("WIDE", 2.0, liquid_fraction=0.1)])
        assert all(result.candidates == 0 for result in shadows)


class TestChainMeasurement:
    def _stats(
        self, contracts: list[Contract], policy: LiquidityPolicy | None = None
    ) -> ChainStats:
        return summarise_chain(
            contracts, now=NOW, window=WINDOW, policy=policy or LiquidityPolicy()
        )

    def test_counts_quoted_contracts(self) -> None:
        stats = self._stats([contract(637), contract(635, quote=False)])
        assert stats.quoted == 1

    def test_counts_greeks_coverage(self) -> None:
        stats = self._stats([contract(637), contract(635, delta=None)])
        assert stats.with_delta == 1

    def test_counts_open_interest_coverage(self) -> None:
        stats = self._stats([contract(637), contract(635, oi=None)])
        assert stats.with_open_interest == 1

    def test_median_spread_is_relative_not_absolute(self) -> None:
        # 1.00/1.10 -> mid 1.05, width 0.10, ~9.5% of mid.
        stats = self._stats([contract(637, bid=1.00, ask=1.10)])
        assert stats.median_spread_pct == pytest.approx(9.52, rel=0.01)

    def test_median_open_interest_is_reported(self) -> None:
        stats = self._stats([contract(637, oi=1000), contract(635, oi=3000)])
        assert stats.median_open_interest == pytest.approx(2000)

    def test_calls_and_puts_are_both_measured_fairly(self) -> None:
        # Screening each contract against its own type matters: screening the
        # whole chain against one type would fail half of it on WRONG_TYPE and
        # halve the apparent liquidity.
        chain = [
            contract(637, kind=ContractType.PUT),
            contract(650, kind=ContractType.CALL),
        ]
        assert self._stats(chain).passing_liquidity == 2

    def test_illiquid_contracts_do_not_pass(self) -> None:
        chain = [
            contract(637),
            contract(635, bid=1.00, ask=2.00),  # spread far too wide
            contract(630, oi=5),  # open interest too low
            contract(625, age_s=300),  # stale quote
            contract(620, quote=False),  # no quote at all
        ]
        assert self._stats(chain).passing_liquidity == 1

    def test_empty_chain_reports_no_medians(self) -> None:
        stats = self._stats([])
        assert stats.median_spread_pct is None
        assert stats.median_open_interest is None
        assert stats.passing_liquidity == 0

    def test_zero_mid_contracts_are_excluded_from_the_spread_median(self) -> None:
        # A zero mid would make the relative spread infinite and poison the
        # median for the whole chain.
        stats = self._stats([contract(637), contract(635, bid=0.0, ask=0.0)])
        assert stats.median_spread_pct is not None
        assert stats.median_spread_pct < 100
