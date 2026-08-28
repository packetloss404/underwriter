"""The tradeable universe and the sector map that connects disclosures to it.

The universe is deliberately fixed and small. Every member has penny-wide,
deeply liquid weekly options, which is the whole point: we run on Basic-plan
data where option quotes are *indicative* rather than OPRA NBBO, and an
indicative quote is only close to the truth where the real market is tight.
A wider universe would buy us more candidates at the cost of trusting quotes
we have no business trusting.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

# GICS-style sectors, named to match how disclosure filings describe holdings.
SECTOR_ENERGY = "energy"
SECTOR_FINANCIALS = "financials"
SECTOR_TECHNOLOGY = "technology"
SECTOR_HEALTHCARE = "healthcare"
SECTOR_INDUSTRIALS = "industrials"
SECTOR_DISCRETIONARY = "consumer_discretionary"
SECTOR_STAPLES = "consumer_staples"
SECTOR_UTILITIES = "utilities"
SECTOR_MATERIALS = "materials"
SECTOR_SEMICONDUCTORS = "semiconductors"
SECTOR_DEFENCE = "aerospace_defence"
SECTOR_BROAD = "broad_market"
SECTOR_RATES = "rates"
SECTOR_GOLD = "gold"


@dataclass(frozen=True, slots=True)
class Instrument:
    """One tradeable ETF.

    `weekly_expiries` records whether the underlying carries Mon/Wed/Fri
    expiries rather than Fridays only. It matters for contract selection: on a
    Friday-only name the 5-14 day window may contain just one or two expiries,
    so the selector has far less freedom and must not treat a thin result as a
    liquidity failure.
    """

    symbol: str
    sector: str
    description: str
    weekly_expiries: bool = False


_UNIVERSE: tuple[Instrument, ...] = (
    # Broad market. SPY and QQQ carry Mon/Wed/Fri expiries with $1-wide strikes.
    Instrument("SPY", SECTOR_BROAD, "S&P 500", weekly_expiries=True),
    Instrument("QQQ", SECTOR_TECHNOLOGY, "Nasdaq 100", weekly_expiries=True),
    Instrument("IWM", SECTOR_BROAD, "Russell 2000", weekly_expiries=True),
    # Sector SPDRs. Friday weeklies, $1-wide strikes.
    Instrument("XLE", SECTOR_ENERGY, "Energy Select Sector"),
    Instrument("XLF", SECTOR_FINANCIALS, "Financial Select Sector"),
    Instrument("XLK", SECTOR_TECHNOLOGY, "Technology Select Sector"),
    Instrument("XLV", SECTOR_HEALTHCARE, "Health Care Select Sector"),
    Instrument("XLI", SECTOR_INDUSTRIALS, "Industrial Select Sector"),
    Instrument("XLY", SECTOR_DISCRETIONARY, "Consumer Discretionary Select Sector"),
    Instrument("XLP", SECTOR_STAPLES, "Consumer Staples Select Sector"),
    Instrument("XLU", SECTOR_UTILITIES, "Utilities Select Sector"),
    Instrument("XLB", SECTOR_MATERIALS, "Materials Select Sector"),
    # Thematic.
    Instrument("SMH", SECTOR_SEMICONDUCTORS, "VanEck Semiconductor"),
    Instrument("ITA", SECTOR_DEFENCE, "iShares Aerospace & Defense"),
    Instrument("TLT", SECTOR_RATES, "iShares 20+ Year Treasury"),
    Instrument("GLD", SECTOR_GOLD, "SPDR Gold Shares"),
)

BY_SYMBOL: MappingProxyType[str, Instrument] = MappingProxyType(
    {inst.symbol: inst for inst in _UNIVERSE}
)

# The ETF a sector view is expressed through. Several sectors share an
# instrument only where the exposure genuinely overlaps.
_SECTOR_TO_SYMBOL: MappingProxyType[str, str] = MappingProxyType(
    {
        SECTOR_ENERGY: "XLE",
        SECTOR_FINANCIALS: "XLF",
        SECTOR_TECHNOLOGY: "XLK",
        SECTOR_HEALTHCARE: "XLV",
        SECTOR_INDUSTRIALS: "XLI",
        SECTOR_DISCRETIONARY: "XLY",
        SECTOR_STAPLES: "XLP",
        SECTOR_UTILITIES: "XLU",
        SECTOR_MATERIALS: "XLB",
        SECTOR_SEMICONDUCTORS: "SMH",
        SECTOR_DEFENCE: "ITA",
        SECTOR_BROAD: "SPY",
        SECTOR_RATES: "TLT",
        SECTOR_GOLD: "GLD",
    }
)

# Instruments whose exposure overlaps enough that holding both is close to one
# bet. The risk engine uses this to stop three "independent" positions from
# being a single leveraged view. Symmetric; stored once, read both ways.
_CORRELATED_PAIRS: frozenset[frozenset[str]] = frozenset(
    {
        frozenset({"SPY", "QQQ"}),
        frozenset({"SPY", "IWM"}),
        frozenset({"SPY", "XLK"}),
        frozenset({"QQQ", "XLK"}),
        frozenset({"QQQ", "SMH"}),
        frozenset({"XLK", "SMH"}),
        frozenset({"XLI", "ITA"}),
        # Rates and gold both trade the real-rate story; not identical, but
        # correlated enough during a macro move to count against the cap.
        frozenset({"TLT", "GLD"}),
    }
)


def symbols() -> tuple[str, ...]:
    return tuple(inst.symbol for inst in _UNIVERSE)


def instrument_for_sector(sector: str) -> Instrument | None:
    """The ETF expressing a sector view, or None if the sector is not tradeable.

    Returning None rather than raising is deliberate: a disclosure can name a
    sector we have no clean instrument for (real estate, telecoms), and that is
    a normal skip with a recorded reason, not an error.
    """
    symbol = _SECTOR_TO_SYMBOL.get(sector)
    return BY_SYMBOL.get(symbol) if symbol else None


def is_tradeable(symbol: str) -> bool:
    return symbol in BY_SYMBOL


def are_correlated(a: str, b: str) -> bool:
    """Whether two instruments overlap enough to count as one bet."""
    return a != b and frozenset({a, b}) in _CORRELATED_PAIRS


def correlated_with(symbol: str) -> frozenset[str]:
    """Every universe member correlated with `symbol`."""
    return frozenset(
        other for pair in _CORRELATED_PAIRS if symbol in pair for other in pair if other != symbol
    )
