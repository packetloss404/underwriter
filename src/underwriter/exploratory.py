"""A broker-isolated counterfactual lane for testing a less selective floor.

This module contains no executor, broker, or order types.  That absence is a
design constraint: an exploratory position can be selected, marked and closed
in the journal, but it cannot be submitted accidentally.
"""

from __future__ import annotations

from typing import Final

from underwriter.journal import ExploratoryPositionRecord
from underwriter.positions import OpenSpread

MIN_VRP_RATIO: Final = 1.05
LIVE_VRP_RATIO: Final = 1.15


def as_open_spread(position: ExploratoryPositionRecord) -> OpenSpread:
    """Adapt a durable hypothetical row to the shared deterministic exit rules."""
    return OpenSpread(
        underlying=position.symbol,
        short_symbol=position.short_symbol,
        long_symbol=position.long_symbol,
        expiry=position.expiry,
        spreads=position.spreads,
        width=position.width,
        credit_per_spread=position.credit_per_spread,
        max_loss=position.max_loss,
        net_delta=position.net_delta,
        unrealised_pnl=position.unrealised_pnl,
        client_order_id=f"exploratory:{position.id}",
        detail=position.detail,
    )
