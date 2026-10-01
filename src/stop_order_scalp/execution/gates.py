"""The safety surface: two opt-in gates and a three-fold interlock.

Every account-changing operation goes through one of these, and neither gate defaults to
open. That default is the whole design: a misconfigured deployment refuses to trade rather
than trading on a configuration nobody read.

============================  =========================================
:class:`OrderGate`            placing, modifying and cancelling orders
:class:`CloseGate`            closing positions
============================  =========================================

Each account-changing operation therefore needs its **own** switch, so that enabling one
does not silently enable the other. And ``LIVE`` additionally needs all three:

1. ``environment == LIVE``
2. ``allow_live``
3. this gate's own ``enabled``

Any two of the three are not enough. Three independent switches on the same path is
deliberate redundancy: it means a single mistake — a copied ``.env``, a forgotten flag, an
environment variable inherited from a shell — does not reach a live account.

:func:`refusal` produces one ordered explanation. Ordering matters for an operator
debugging a refusal at 3am: the *first* missing thing is the one to fix, and a message
listing all of them in arbitrary order wastes that. So the checks run cheapest-first and
most-fundamental-first, and the first failure is returned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from stop_order_scalp.domain.enums import Environment
from stop_order_scalp.domain.exceptions import ConfigError
from stop_order_scalp.domain.models import EnvironmentSettings

__all__ = ["CloseGate", "Gate", "GateDecision", "GateRefusal", "OrderGate"]


class GateRefusal:
    """Stable reasons a gate refuses, as plain strings.

    An interface, like :class:`~stop_order_scalp.risk.risk_manager.RejectionCode`: a
    refusal that only says "not allowed" forces every caller to parse prose.
    """

    DISABLED: str = "gate_disabled"
    NOT_LIVE: str = "environment_not_live"
    ALLOW_LIVE_MISSING: str = "allow_live_false"
    ALLOW_OPERATION_MISSING: str = "allow_operation_false"
    DEMO_REQUIRED: str = "demo_only"


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Whether the gate opens, and why not if it does not."""

    open: bool
    code: str = ""
    reason: str = ""

    @property
    def refused(self) -> bool:
        return not self.open

    def require(self) -> None:
        """Raise unless the gate is open.

        Called by the operation itself rather than by every caller, so there is no path
        that reaches a broker without passing through here.
        """
        if not self.open:
            raise PermissionError(f"{self.code}: {self.reason}")

    def __bool__(self) -> bool:
        return self.open

    def __str__(self) -> str:
        return f"GateDecision({'open' if self.open else self.code})"


OPEN: GateDecision = GateDecision(open=True)


@runtime_checkable
class Gate(Protocol):
    """What both gates share."""

    @property
    def enabled(self) -> bool:
        """Whether this gate's own switch is on. ``False`` by default, always."""

    def check(self, settings: EnvironmentSettings) -> GateDecision: ...


def refusal(code: str, reason: str) -> GateDecision:
    return GateDecision(open=False, code=code, reason=reason)


@dataclass(frozen=True, slots=True)
class OrderGate:
    """Opt-in for placing, modifying and cancelling orders.

    ``enabled`` defaults to ``False`` and is not derivable from anything else: it is a
    separate switch precisely so that ``SOS_ALLOW_ORDER`` cannot be satisfied on its own.
    """

    enabled: bool = False

    def check(self, settings: EnvironmentSettings) -> GateDecision:
        """Evaluate the gate. Returns a decision rather than raising.

        Ordered cheapest-first, and most-fundamental-first, so the returned reason is the
        one an operator should act on:

        1. the gate's own switch, because everything else is moot without it;
        2. the environment, because a gate open in ``DRY_RUN`` is simply not reached;
        3. ``allow_live``, which is what makes ``LIVE`` reachable at all;
        4. ``allow_order``, this operation's own opt-in.

        In ``DRY_RUN`` the answer is a refusal unless the gate is off *and* nothing asked
        for it -- so a caller that wants to place an order must have opened the gate on
        purpose. That is the fail-closed direction.
        """
        if not self.enabled:
            return refusal(
                GateRefusal.DISABLED,
                "order gate is disabled; set order_gate.enabled (SOS_ALLOW_ORDER) on "
                "purpose. Every account-changing operation needs its own switch.",
            )
        if settings.environment is not Environment.LIVE:
            return refusal(
                GateRefusal.NOT_LIVE,
                f"environment is {settings.environment}; order gate only opens for LIVE. "
                "DRY_RUN and PAPER use SimulatedBroker and never reach this gate.",
            )
        if not settings.allow_live:
            return refusal(
                GateRefusal.ALLOW_LIVE_MISSING,
                "SOS_ALLOW_LIVE is false; LIVE requires it independently of any "
                "per-operation switch",
            )
        if not settings.allow_order:
            return refusal(
                GateRefusal.ALLOW_OPERATION_MISSING,
                "SOS_ALLOW_ORDER is false; placing orders requires its own opt-in even "
                "with SOS_ALLOW_LIVE set",
            )
        return OPEN

    def require(self, settings: EnvironmentSettings) -> None:
        self.check(settings).require()

    def __str__(self) -> str:
        return f"OrderGate(enabled={self.enabled})"


@dataclass(frozen=True, slots=True)
class CloseGate:
    """Opt-in for closing positions.

    Separate from :class:`OrderGate` because placing and closing are different risks. An
    operator who is content to open positions is not automatically content to have them
    closed automatically -- and on a losing streak, automatic closing is exactly when it
    would fire.
    """

    enabled: bool = False

    def check(self, settings: EnvironmentSettings) -> GateDecision:
        """Evaluate the gate, in the same order and for the same reasons as the order gate."""
        if not self.enabled:
            return refusal(
                GateRefusal.DISABLED,
                "close gate is disabled; set close_gate.enabled (SOS_ALLOW_CLOSE) on "
                "purpose. Closing positions has its own switch, separate from opening them.",
            )
        if settings.environment is not Environment.LIVE:
            return refusal(
                GateRefusal.NOT_LIVE,
                f"environment is {settings.environment}; close gate only opens for LIVE",
            )
        if not settings.allow_live:
            return refusal(
                GateRefusal.ALLOW_LIVE_MISSING,
                "SOS_ALLOW_LIVE is false; LIVE requires it independently",
            )
        if not settings.allow_close:
            return refusal(
                GateRefusal.ALLOW_OPERATION_MISSING,
                "SOS_ALLOW_CLOSE is false; closing positions requires its own opt-in even "
                "with SOS_ALLOW_ORDER and SOS_ALLOW_LIVE set",
            )
        return OPEN

    def require(self, settings: EnvironmentSettings) -> None:
        self.check(settings).require()

    def __str__(self) -> str:
        return f"CloseGate(enabled={self.enabled})"


@dataclass(frozen=True, slots=True)
class LiveInterlock:
    """The three-fold ``LIVE`` check, in one place.

    Separate from the gates because it answers a different question -- *may this deployment
    reach a live account at all* -- and because the answer belongs to configuration rather
    than to an operation. Every gate consults it, so there is one implementation of
    "reachable" rather than three that could drift.
    """

    environment: Environment = Environment.DRY_RUN
    allow_live: bool = False

    @classmethod
    def from_settings(cls, settings: EnvironmentSettings) -> LiveInterlock:
        return cls(environment=settings.environment, allow_live=settings.allow_live)

    @property
    def live_reachable(self) -> bool:
        return self.environment is Environment.LIVE and self.allow_live

    def check(self) -> GateDecision:
        if self.environment is not Environment.LIVE:
            return refusal(
                GateRefusal.NOT_LIVE,
                f"environment is {self.environment}, not LIVE",
            )
        if not self.allow_live:
            return refusal(
                GateRefusal.ALLOW_LIVE_MISSING,
                "SOS_ALLOW_LIVE is false",
            )
        return OPEN

    def as_dict(self) -> dict[str, Any]:
        return {
            "environment": str(self.environment),
            "allow_live": self.allow_live,
            "live_reachable": self.live_reachable,
        }

    def __str__(self) -> str:
        return f"LiveInterlock({self.environment}, allow_live={self.allow_live})"


def describe_safety(settings: EnvironmentSettings, order: OrderGate, close: CloseGate) -> str:
    """A one-line summary for ``status``, naming every switch and where it stands.

    Printed by the CLI so an operator can see the whole safety surface at once, rather than
    discovering one switch is off by hitting it.
    """
    interlock = LiveInterlock.from_settings(settings)
    return (
        f"environment={settings.environment} "
        f"allow_live={settings.allow_live} "
        f"order_gate={order.enabled} "
        f"close_gate={close.enabled} "
        f"live_reachable={interlock.live_reachable}"
    )


def require_configured(settings: EnvironmentSettings) -> None:
    """Refuse a configuration that claims LIVE without the switches that back it.

    :raises ConfigError: for a configuration that is internally inconsistent. Caught at
        load rather than at the first order, so an impossible combination is reported
        before it matters.
    """
    if settings.environment is Environment.LIVE and not settings.allow_live:
        raise ConfigError(
            "SOS_ENVIRONMENT=LIVE requires SOS_ALLOW_LIVE=true; the two are independent "
            "and neither implies the other"
        )
    if settings.allow_live and settings.environment is not Environment.LIVE:
        # Not an error: enabling the interlock in DRY_RUN is a reasonable way to stage a
        # deployment. Worth stating so nobody assumes it did something.
        return
