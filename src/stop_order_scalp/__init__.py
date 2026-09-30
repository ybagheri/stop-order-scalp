"""Stop Order Scalp — a US30 stop-order scalping system.

The package is layered so that dependencies point strictly inward:

``domain``
    Immutable value objects, enums, exceptions and protocols. No I/O, no MetaTrader 5,
    no floats for money.
``market_data``
    Timeframes, candles, ticks, symbol specifications and the MetaTrader 5 feed.
``strategy`` / ``risk`` / ``trailing`` / ``lifecycle``
    Pure business logic. These layers never import MetaTrader 5.
``execution``
    The ``Broker`` protocol plus its MetaTrader 5 and simulated implementations.
``infrastructure``
    Configuration, structured logging, persistence, clock.
``integrations``
    Optional third-party adapters (Al Brooks Price Action Engine).
``application``
    Composition and orchestration.
``backtest`` / ``research``
    Replay and parameter research, both isolated from live execution.
``cli``
    The command-line entry point and the composition root.

Only ``execution.mt5_broker`` and ``market_data.mt5_feed`` may import ``MetaTrader5``,
and only lazily. ``scripts/check_architecture.py`` enforces this.
"""

from stop_order_scalp.domain.version import __version__

__all__ = ["__version__"]
