"""What the system is doing right now, and what it would resume into.

Separate from :mod:`stop_order_scalp.application.diagnostics` on purpose, and the split is
about risk rather than tidiness. ``diagnostics`` is the command people reach for when
something is wrong, and it has to be safe on a machine that is not ready: it connects
read-only and never reconciles an order. ``status`` runs the lifecycle's recovery pass, so it
*does* change state -- which is what makes its answer trustworthy, and what would make it the
wrong thing to reach for while diagnosing.

The report is built from a **dry run**, deliberately. A status that touched a real venue
would be a read that could place an order, and that is a surprising thing for a command named
"status" to do. What it can tell you about a live account, it tells you by *reading* the
venue, which is `diagnostics`' job.
"""

from __future__ import annotations

from typing import Any

from stop_order_scalp.domain.interfaces import Clock
from stop_order_scalp.infrastructure.clock import SystemClock
from stop_order_scalp.infrastructure.config import AppConfig

__all__ = ["collect_status"]


def collect_status(
    config: AppConfig, *, clock: Clock | None = None
) -> dict[str, Any]:
    """What the system is doing now, and what it would resume into.

    ``clock`` is injected rather than read from the wall, because the architecture gate forbids
    ``datetime.now()`` outside the infrastructure layer and the reason applies here too: a
    report whose timestamp cannot be substituted is a report that cannot be reproduced. The
    default is :class:`SystemClock`, which is the one component allowed to read real time.
    """
    now = (clock or SystemClock()).now()
    """What the system is doing now, and what it would resume into.

    Unlike :func:`build_diagnostics` this runs the lifecycle's recovery pass, so an intent
    left unresolved by a previous process is reported rather than ignored. That is the whole
    point of asking: the answer is "there is an intent from Tuesday that I cannot classify, go
    and look at the broker", not silence.
    """
    from stop_order_scalp.application.service import build_service
    from stop_order_scalp.domain.exceptions import BrokerError

    report: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "environment": str(config.environment.environment),
        "symbol": config.strategy.symbol,
        # Always true: the venue this status is built from is a simulated one, so it is
        # reachable whatever the state of any terminal. The key is part of the CLI's exit-code
        # contract, and naming it "reachable" while a real venue may be unreachable would be
        # misleading -- so it says what it means.
        "reachable": True,
        "reachable_note": (
            "This status is built against a simulated venue, so it is reachable by "
            "definition. Whether the real terminal is reachable is `diagnostics`, which "
            "connects read-only."
        ),
    }

    try:
        service = build_service(config, dry_run=True)
    except BrokerError as exc:  # pragma: no cover - a dry run needs no broker
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report

    report["lifecycle_state"] = str(service.lifecycle.state)
    report["open_positions"] = len(service.broker.positions())
    report["working_orders"] = len(service.broker.orders())
    report["balance"] = str(service.broker.account().balance.amount)
    report["ledger"] = {
        "entries": len(service.lifecycle.ledger),
        "awaiting_confirmation": len(service.lifecycle.ledger.awaiting_confirmation()),
        "note": (
            "A dry run keeps its ledger in memory, so this counts nothing across processes. "
            "It is populated only when state.path points at a file for the trading path."
        ),
    }
    report["live_ledger"] = str(config.paths.state_file)
    return report
