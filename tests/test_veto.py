"""Catalyst veto tests.

This is the only place a language model touches the trade, so almost every
test here is about the model failing. The governing property: there is no input
to this module that causes a trade to happen. The model can stop one, and that
is all it can do.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime

import pytest

from underwriter.chain import Contract, ContractType, CreditSpread, Quote
from underwriter.veto import (
    MAX_HEADLINE_CHARS,
    SYSTEM_PROMPT,
    CatalystVeto,
    _build_prompt,
    _parse,
)
from underwriter.volatility import VolRanking

NOW = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)
RANKING = VolRanking(symbol="XLE", implied_vol=0.24, realised_vol=0.154, realised_vol_context=0.18)


def leg(strike: float, delta: float) -> Contract:
    return Contract(
        symbol=f"XLE260911P{int(strike * 1000):08d}",
        underlying="XLE",
        expiry=date(2026, 9, 11),
        strike=strike,
        contract_type=ContractType.PUT,
        quote=Quote(0.20, 0.25, NOW),
        delta=delta,
        open_interest=900,
    )


SPREAD = CreditSpread(
    short_leg=leg(61, -0.25),
    long_leg=leg(58, -0.12),
    credit=0.31,
    contract_type=ContractType.PUT,
)


class FakeModel:
    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeNews:
    def __init__(self, items: Sequence[str] | Exception) -> None:
        self.items = items

    def headlines(self, symbol: str, *, since: datetime) -> Sequence[str]:
        if isinstance(self.items, Exception):
            raise self.items
        return self.items


def screen(response: str | Exception, news: object | None = None):  # type: ignore[no-untyped-def]
    veto = CatalystVeto(
        model=FakeModel(response),
        news=news,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    return veto.screen(symbol="XLE", ranking=RANKING, spread=SPREAD)


class TestTheOnlyPathToClear:
    def test_a_well_formed_no_clears(self) -> None:
        v = screen('{"veto": false, "catalyst": "", "reason": "no dated event"}')
        assert not v.vetoed

    def test_a_well_formed_yes_vetoes(self) -> None:
        v = screen('{"veto": true, "catalyst": "OPEC+ meeting", "reason": "supply"}')
        assert v.vetoed
        assert v.catalyst == "OPEC+ meeting"

    def test_a_fenced_response_is_still_read(self) -> None:
        # Models wrap JSON in fences constantly; refusing to parse that would
        # veto every trade for a formatting habit.
        v = screen('```json\n{"veto": true, "catalyst": "CPI print"}\n```')
        assert v.vetoed
        assert v.catalyst == "CPI print"

    def test_surrounding_prose_is_tolerated(self) -> None:
        v = screen('Sure! {"veto": false, "reason": "nothing scheduled"} Hope that helps.')
        assert not v.vetoed


class TestEveryFailureVetoes:
    @pytest.mark.parametrize(
        "response",
        [
            "",
            "I think energy looks risky right now.",
            "{",
            '{"veto": tru',
            "[]",
            '"just a string"',
            '{"catalyst": "something", "reason": "x"}',
            '{"veto": "yes"}',
            '{"veto": 1}',
            '{"veto": null}',
        ],
    )
    def test_an_unreadable_answer_vetoes(self, response: str) -> None:
        # An answer we cannot read is not an answer. Treating it as approval
        # would make the model's worst day our worst day.
        assert screen(response).vetoed

    @pytest.mark.parametrize(
        "failure",
        [TimeoutError("upstream"), ConnectionError("dns"), RuntimeError("429"), ValueError("x")],
    )
    def test_an_unavailable_model_vetoes(self, failure: Exception) -> None:
        v = screen(failure)
        assert v.vetoed
        assert "vetoing rather than trading unscreened" in v.detail

    def test_a_veto_without_a_named_catalyst_is_still_a_veto(self) -> None:
        # We never override the model toward trading -- but the audit trail
        # should record that it was vague.
        v = screen('{"veto": true, "reason": "feels risky"}')
        assert v.vetoed
        assert v.catalyst == "unnamed"


class TestNewsIsNotLoadBearing:
    def test_headlines_reach_the_prompt(self) -> None:
        model = FakeModel('{"veto": false}')
        CatalystVeto(model=model, news=FakeNews(["OPEC meets Thursday"]), clock=lambda: NOW).screen(
            symbol="XLE", ranking=RANKING, spread=SPREAD
        )
        assert "OPEC meets Thursday" in model.calls[0][1]

    def test_a_failing_news_source_does_not_veto(self) -> None:
        # News being down is not itself evidence of a catalyst; the model can
        # still reason from the volatility figures and its own calendar.
        assert not screen('{"veto": false}', news=FakeNews(ConnectionError("down"))).vetoed

    def test_no_news_source_still_screens(self) -> None:
        assert not screen('{"veto": false}', news=None).vetoed


class TestThePromptWithholdsOurBook:
    def _user_prompt(self) -> str:
        model = FakeModel('{"veto": false}')
        CatalystVeto(model=model, news=None, clock=lambda: NOW).screen(
            symbol="XLE", ranking=RANKING, spread=SPREAD
        )
        return model.calls[0][1]

    def test_the_model_never_sees_strikes_or_size(self) -> None:
        # It is answering a question about the world, not about our book.
        # Showing it our position would invite an opinion it is not qualified
        # to have, and give a prompt injection something to aim at.
        prompt = self._user_prompt()
        for leaked in ("61", "58", "0.31", "credit", "strike", "contracts"):
            assert leaked not in prompt.lower(), leaked

    def test_it_does_see_the_volatility_it_is_asked_about(self) -> None:
        prompt = self._user_prompt()
        assert "24.0%" in prompt
        assert "XLE" in prompt

    def test_headlines_are_truncated(self) -> None:
        long_headline = "x" * 5000
        model = FakeModel('{"veto": false}')
        CatalystVeto(model=model, news=FakeNews([long_headline]), clock=lambda: NOW).screen(
            symbol="XLE", ranking=RANKING, spread=SPREAD
        )
        assert len(model.calls[0][1]) < 5000

    def test_the_system_prompt_marks_headlines_untrusted(self) -> None:
        assert "untrusted" in SYSTEM_PROMPT.lower()
        assert "not instructions" in SYSTEM_PROMPT.lower()


class TestParsing:
    def test_fields_are_truncated_rather_than_trusted(self) -> None:
        v = _parse('{"veto": true, "catalyst": "' + "y" * 900 + '", "reason": "' + "z" * 900 + '"}')
        assert len(v.catalyst) <= 200
        assert len(v.detail) <= 300

    def test_a_prompt_carries_the_date_so_the_model_can_judge_recency(self) -> None:
        assert "2026-08-31" in _build_prompt("XLE", RANKING, [], NOW)

    def test_headline_cap_is_applied_at_source(self) -> None:
        assert MAX_HEADLINE_CHARS < 1000
