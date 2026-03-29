"""
trading_engine/core/portfolio.py
Portfolio state dataclasses and snapshot builder.

Builds a clean PortfolioState from raw Webull positions + cash,
calculates actual weights, deltas vs targets, and P&L.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from config.settings import (
    ENGINE_TARGETS,
    AGGRESSIVE_TICKERS, MODERATE_TICKERS, SAFE_TICKERS,
    ALL_TICKER_WEIGHTS,
)


# ---------------------------------------------------------------------------
# Which engine owns each ticker
# ---------------------------------------------------------------------------
TICKER_ENGINE: Dict[str, str] = {
    **{t: "aggressive" for t in AGGRESSIVE_TICKERS},
    **{t: "moderate"   for t in MODERATE_TICKERS},
    **{t: "safe"       for t in SAFE_TICKERS},
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TickerPosition:
    ticker:          str
    engine:          str
    shares:          float
    avg_cost:        float
    current_price:   float
    market_value:    float
    unrealized_pnl:  float
    unrealized_pct:  float

    # Computed after snapshot is assembled
    actual_pct:      float = 0.0   # % of total portfolio
    target_pct:      float = 0.0   # target % of total portfolio
    delta_pct:       float = 0.0   # actual - target (positive = overweight)

    @property
    def is_overweight(self) -> bool:
        return self.delta_pct > 0

    @property
    def is_underweight(self) -> bool:
        return self.delta_pct < 0


@dataclass
class EngineAllocation:
    engine:       str
    target_pct:   float
    actual_pct:   float = 0.0
    actual_value: float = 0.0
    delta_pct:    float = 0.0   # actual - target

    @property
    def is_overweight(self) -> bool:
        return self.delta_pct > 0

    @property
    def is_underweight(self) -> bool:
        return self.delta_pct < 0


@dataclass
class PortfolioState:
    snapshot_date:    datetime.date
    portfolio_value:  float
    cash_balance:     float
    cash_pct:         float

    positions:        Dict[str, TickerPosition]   = field(default_factory=dict)
    engine_allocs:    Dict[str, EngineAllocation] = field(default_factory=dict)

    # Risk
    peak_value:       float = 0.0
    drawdown_pct:     float = 0.0

    # Trade counters (filled in by risk state module)
    trades_today:     int = 0
    trades_this_week: int = 0

    def get_position(self, ticker: str) -> Optional[TickerPosition]:
        return self.positions.get(ticker)

    def market_value_of(self, ticker: str) -> float:
        pos = self.positions.get(ticker)
        return pos.market_value if pos else 0.0

    def actual_pct_of(self, ticker: str) -> float:
        pos = self.positions.get(ticker)
        return pos.actual_pct if pos else 0.0

    def target_pct_of(self, ticker: str) -> float:
        return ALL_TICKER_WEIGHTS.get(ticker, 0.0)

    def engine_actual_pct(self, engine: str) -> float:
        alloc = self.engine_allocs.get(engine)
        return alloc.actual_pct if alloc else 0.0

    def overweight_tickers(self, threshold: float = 0.02) -> List[TickerPosition]:
        return [p for p in self.positions.values() if p.delta_pct >= threshold]

    def underweight_tickers(self, threshold: float = 0.02) -> List[TickerPosition]:
        return [p for p in self.positions.values() if p.delta_pct <= -threshold]

    def tickers_by_engine(self, engine: str) -> List[TickerPosition]:
        return [p for p in self.positions.values() if p.engine == engine]
