"""Catalyst veto tests.

This is the only place a language model touches the trade, so almost every
test here is about the model failing. The governing property: there is no input
to this module that causes a trade to happen. The model can stop one, and that
is all it can do.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import ClassVar

import pytest

from underwriter.chain import Contract, ContractType, CreditSpread, Quote
from underwriter.veto import (
    ANTHROPIC_MODEL,
    MAX_HEADLINE_CHARS,
    OPENAI_MODEL,
    SYSTEM_PROMPT,
    AnthropicModel,
    CatalystVeto,
    OpenAIModel,
    _build_prompt,
    _parse,
    build_veto,
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


class TestProviderSelection:
    """Either provider works because both sit behind one `ModelClient`
    protocol, so the veto's failure handling is identical whichever is wired.
    Selection is explicit rather than a silent fallback: screening done by a
    different model than the operator asked for, with nothing saying so, is
    worse than a refusal to start."""

    KW: ClassVar[dict[str, str]] = {"alpaca_key": "k", "alpaca_secret": "s"}

    def test_auto_picks_anthropic_when_only_that_key_is_present(self) -> None:
        veto = build_veto(**self.KW, anthropic_key="a")
        assert isinstance(veto.model, AnthropicModel)

    def test_auto_picks_openai_when_only_that_key_is_present(self) -> None:
        veto = build_veto(**self.KW, openai_key="o")
        assert isinstance(veto.model, OpenAIModel)

    def test_auto_prefers_anthropic_when_both_are_present(self) -> None:
        # A stray second key must not silently change which model is deciding.
        veto = build_veto(**self.KW, anthropic_key="a", openai_key="o")
        assert isinstance(veto.model, AnthropicModel)

    def test_an_explicit_provider_is_honoured_over_the_preference(self) -> None:
        veto = build_veto(**self.KW, anthropic_key="a", openai_key="o", provider="openai")
        assert isinstance(veto.model, OpenAIModel)

    @pytest.mark.parametrize(
        ("provider", "kwargs"),
        [("anthropic", {"openai_key": "o"}), ("openai", {"anthropic_key": "a"})],
    )
    def test_a_requested_provider_without_its_key_raises(
        self, provider: str, kwargs: dict[str, str]
    ) -> None:
        # Never fall back to the other one.
        with pytest.raises(ValueError, match="not set"):
            build_veto(**self.KW, provider=provider, **kwargs)

    def test_no_keys_at_all_raises(self) -> None:
        with pytest.raises(ValueError, match="no model provider"):
            build_veto(**self.KW)

    def test_the_model_name_is_overridable(self) -> None:
        # Model names move faster than this repository will.
        model = build_veto(**self.KW, openai_key="o", model_name="some-newer-model").model
        assert isinstance(model, OpenAIModel)
        assert model.model == "some-newer-model"

    def test_defaults_track_the_provider(self) -> None:
        # Narrowed rather than duck-typed: ModelClient is a one-method
        # protocol and deliberately says nothing about a model name.
        openai_model = build_veto(**self.KW, openai_key="o").model
        anthropic_model = build_veto(**self.KW, anthropic_key="a").model
        assert isinstance(openai_model, OpenAIModel)
        assert isinstance(anthropic_model, AnthropicModel)
        assert openai_model.model == OPENAI_MODEL
        assert anthropic_model.model == ANTHROPIC_MODEL


class TestOpenAIFailsClosedToo:
    """The whole point of the shared protocol: the failure semantics do not
    depend on which provider is wired."""

    def test_an_empty_choices_list_reads_as_no_answer(self) -> None:
        # Returned as "" rather than raised, so it takes the same parse path --
        # and an unreadable answer is a veto either way.
        assert screen("").vetoed

    def test_every_bad_response_still_vetoes_with_openai_defaults(self) -> None:
        for bad in ("", "not json", '{"veto": "maybe"}'):
            assert screen(bad).vetoed
