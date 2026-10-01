"""The risk engine's verdict on a prospective trade.

:meth:`RiskManager.assess` takes a signal and answers one question: *may this be traded,
and at what size?* The answer is a :class:`~stop_order_scalp.domain.models.RiskAssessment`
carrying either the sized volumes or a **machine-readable rejection code**.

The codes matter more than they look. A rejection that only says "too risky" forces every
caller to parse prose to decide what to do next. A code can be counted, alerted on, and
compared across runs, and it is the difference between "the engine rejected 12 % of
signals today" and "the engine rejected 12 signals, all ``below_min_volume``".

Every rejection path here returns rather than raising, with one deliberate exception:
anything that indicates the *inputs* are wrong rather than the *trade* is worth stopping
for, and a misconfigured risk engine that silently rejects everything is worse than one
that says so.

The order of validation is the order a reviewer would want to read, and it is cheapest
first -- there is no point computing a size that a volume bound will reject.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from stop_order_scalp.domain.enums import Side
from stop_order_scalp.domain.exceptions import InvalidSpecificationError, RiskError
from stop_order_scalp.domain.models import (
    AccountSnapshot,
    RiskAssessment,
    TradePlan,
    TradeSignal,
)
from stop_order_scalp.domain.value_objects import Price, SymbolSpecification
from stop_order_scalp.infrastructure.config import (
    RiskSettings,
    TargetSettings,
)
from stop_order_scalp.risk.commission import CommissionModel_
from stop_order_scalp.risk.position_sizer import SizingInput, SizingResult, size_position
from stop_order_scalp.risk.stop_loss import (
    FixedPointsStopProvider,
    SignalStopProvider,
    StopLevel,
    StopLossProvider,
    default_stop_provider,
)
from stop_order_scalp.risk.take_profit import (
    FixedPointsTargetProvider,
    TakeProfitProvider,
    TargetLevel,
    default_target_provider,
    resolve_target,
)

__all__ = ["RejectionCode", "RiskManager", "RiskRequest"]


class RejectionCode:
    """Stable, machine-readable rejection reasons.

    Plain string constants rather than an enum, so a code can be compared with ``==``
    against a value in a log or a metric without an import.

    **These strings are an interface.** Renaming one breaks every alert and every
    dashboard built on it, so add a code rather than reword one.
    """

    ACCOUNT_NOT_TRADEABLE: str = "account_not_tradeable"
    BALANCE_NOT_POSITIVE: str = "balance_not_positive"
    NO_STOP: str = "no_stop_loss"
    STOP_ON_WRONG_SIDE: str = "stop_on_wrong_side"
    STOP_TOO_CLOSE: str = "stop_too_close"
    BELOW_MIN_VOLUME: str = "below_min_volume"
    ABOVE_MAX_RISK: str = "above_max_total_risk"
    VOLUME_ABOVE_MAX: str = "volume_above_broker_max"
    SPECIFICATION_UNUSABLE: str = "specification_unusable"
    ZERO_COST_PER_LOT: str = "zero_cost_per_lot"


@dataclass(frozen=True, slots=True)
class RiskRequest:
    """Everything the risk engine needs for one decision."""

    signal: TradeSignal
    account: AccountSnapshot
    specification: SymbolSpecification
    risk: RiskSettings
    target: TargetSettings
    #: The provider the configured target mode describes. Injectable so a test does not
    #: have to reconstruct configuration to exercise a rule.
    target_provider: TakeProfitProvider | None = None
    #: Extra context carried into the plan's log, never executed.
    context: dict[str, object] = field(default_factory=dict)


class RiskManager:
    """Sizes and validates a prospective trade.

    Stateless. Every input arrives per call, so a single instance can serve the live loop,
    the backtester and a test without carrying state between them.
    """

    __slots__ = ("_risk", "_stop_provider", "_target", "_target_provider")

    def __init__(
        self,
        risk: RiskSettings,
        target: TargetSettings,
        *,
        stop_provider: StopLossProvider | None = None,
        target_provider: TakeProfitProvider | None = None,
    ) -> None:
        self._risk = risk
        self._target = target
        self._stop_provider = stop_provider or default_stop_provider(target.stop_loss_points)
        self._target_provider = target_provider or default_target_provider(
            target.mode,
            take_profit_points=target.take_profit_points,
            risk_reward=target.risk_reward,
        )

    @property
    def risk_settings(self) -> RiskSettings:
        return self._risk

    @property
    def target_settings(self) -> TargetSettings:
        """The target settings in force.

        Exposed so a caller that re-assesses against live state -- the order manager does
        exactly that -- uses the *same* settings the manager was built with, rather than
        reconstructing defaults that could differ.
        """
        return self._target

    @property
    def stop_provider(self) -> StopLossProvider:
        return self._stop_provider

    @property
    def target_provider(self) -> TakeProfitProvider:
        return self._target_provider

    @property
    def commission(self) -> CommissionModel_:
        """The resolved commission model, exposed so sizing and reporting cannot diverge."""
        return CommissionModel_(
            rate=self._risk.commission_per_lot,
            mode=self._risk.commission_mode,
            currency="USD",
        )

    def assess(self, request: RiskRequest) -> RiskAssessment:
        """Size and validate, returning the verdict rather than raising for a bad trade."""
        signal = request.signal
        specification = request.specification

        if not request.account.trade_allowed:
            return RiskAssessment.reject(
                RejectionCode.ACCOUNT_NOT_TRADEABLE,
                "the account reports trading disabled; check the terminal's trading tab",
            )
        if request.account.balance.amount <= 0:
            return RiskAssessment.reject(
                RejectionCode.BALANCE_NOT_POSITIVE,
                f"account balance is {request.account.balance.amount}",
            )

        resolved = self._resolve_stop(signal, specification)
        if isinstance(resolved, RiskAssessment):
            return resolved
        stop = resolved

        entry = signal.reference_price
        try:
            sizing = size_position(_sizing_input(request, entry, stop.price))
        except RiskError as exc:
            return RiskAssessment.reject(*_classify(exc))
        except InvalidSpecificationError as exc:
            return RiskAssessment.reject(RejectionCode.SPECIFICATION_UNUSABLE, str(exc))

        refusal = self._check_risk_ceiling(sizing, request.specification)
        if refusal is not None:
            return refusal

        target_level = self._resolve_target(signal, entry, stop, specification)
        plan = self._build_plan(signal, entry, stop, target_level, sizing)
        return RiskAssessment(
            accepted=True,
            reason=(
                f"{plan.volume.lots} lots, total risk {plan.total_risk.amount} "
                f"({sizing.risk_fraction}% of balance)"
            ),
            code="ok",
            volume=plan.volume,
            price_risk=plan.price_risk,
            commission=plan.commission,
            total_risk=plan.total_risk,
            risk_fraction=sizing.risk_fraction,
        )

    def plan_for(self, request: RiskRequest) -> TradePlan:
        """The full plan, for a caller that needs it rather than just the verdict.

        :raises RiskError: when the trade is not acceptable. The ``code`` that
            :meth:`assess` would have reported is attached to the exception's message, so a
            caller that catches it can still log something machine-readable.
        """
        assessment = self.assess(request)
        if not assessment.accepted:
            raise RiskError(f"{assessment.code}: {assessment.reason}")
        entry = request.signal.reference_price
        stop = self._resolve_stop(request.signal, request.specification)
        target_level = self._resolve_target(
            request.signal, entry, stop, request.specification  # type: ignore[arg-type]
        )
        sizing = size_position(_sizing_input(request, entry, stop.price))  # type: ignore[union-attr]
        return self._build_plan(request.signal, entry, stop, target_level, sizing)  # type: ignore[arg-type]

    # --- steps ----------------------------------------------------------

    def _resolve_stop(
        self, signal: TradeSignal, specification: SymbolSpecification
    ) -> StopLevel | RiskAssessment:
        supplied = signal.stop_loss
        if supplied is None:
            level = self._stop_provider.stop_for(
                signal.reference_price, signal.side, specification
            )
        else:
            fallback = FixedPointsStopProvider(self._target.stop_loss_points)
            provider = SignalStopProvider(self._target.stop_loss_points, fallback)
            try:
                level = provider.stop_for(
                    signal.reference_price, signal.side, specification, supplied=supplied
                )
            except RiskError as exc:
                return RiskAssessment.reject(RejectionCode.STOP_ON_WRONG_SIDE, str(exc))
        return self._check_stop_distance(level, signal.side, specification)

    def _check_stop_distance(
        self, level: StopLevel, side: Side, specification: SymbolSpecification
    ) -> StopLevel | RiskAssessment:
        """The broker's ``stops_level`` is a hard constraint, not advice.

        A stop closer to market than ``stops_level`` is rejected by the broker, and the
        rejection arrives at send time -- after the size has been committed. Checking here
        means the trade is never sized.
        """
        del side
        if specification.stops_level and level.distance_points < specification.stops_level:
            return RiskAssessment.reject(
                RejectionCode.STOP_TOO_CLOSE,
                f"stop is {level.distance_points} points from entry but the broker "
                f"requires at least {specification.stops_level}",
            )
        return level

    def _resolve_target(
        self,
        signal: TradeSignal,
        entry: Price,
        stop: StopLevel,
        specification: SymbolSpecification,
    ) -> TargetLevel:
        try:
            return resolve_target(
                entry=entry,
                side=signal.side,
                stop=stop.price,
                specification=specification,
                configured=self._target_provider,
                supplied=signal.take_profit,
            )
        except RiskError:
            # A target problem must not stop a trade whose risk is already bounded; fall
            # back to the configured fixed-point target rather than refusing.
            return FixedPointsTargetProvider(self._target.take_profit_points).target_for(
                entry, signal.side, specification
            )

    def _check_risk_ceiling(
        self, sizing: SizingResult, specification: SymbolSpecification
    ) -> RiskAssessment | None:
        """The two independent brakes, checked after sizing.

        ``max_total_risk_fraction`` is deliberately independent of ``percent``: it is a
        second ceiling that a mistake in the first cannot widen.
        """
        ceiling = self._risk.max_total_risk_fraction * Decimal(100)
        if sizing.risk_fraction > ceiling:
            return RiskAssessment.reject(
                RejectionCode.ABOVE_MAX_RISK,
                f"total risk {sizing.risk_fraction}% exceeds the configured ceiling "
                f"{ceiling}%",
            )
        if sizing.volume.lots > specification.volume_max:
            return RiskAssessment.reject(
                RejectionCode.VOLUME_ABOVE_MAX,
                f"{sizing.volume.lots} lots exceeds the broker maximum "
                f"{specification.volume_max}",
            )
        return None

    def _build_plan(
        self,
        signal: TradeSignal,
        entry: Price,
        stop: StopLevel,
        target: TargetLevel,
        sizing: SizingResult,
    ) -> TradePlan:
        return TradePlan(
            plan_id=_plan_id(signal),
            signal=signal,
            entry=entry,
            stop_loss=stop.price,
            take_profit=target.price,
            volume=sizing.volume,
            price_risk=sizing.price_risk,
            commission=sizing.commission,
            total_risk=sizing.total_risk,
            target_mode=target.mode,
            created_at=signal.source_candle_open_time,
        )


# =============================================================================
# Helpers
# =============================================================================


def _sizing_input(request: RiskRequest, entry: Price, stop: Price) -> SizingInput:
    return SizingInput(
        entry=entry,
        stop=stop,
        balance=request.account.balance,
        specification=request.specification,
        mode=request.risk.mode,
        percent=request.risk.percent,
        fixed_lot=request.risk.fixed_lot,
        commission=CommissionModel_(
            rate=request.risk.commission_per_lot,
            mode=request.risk.commission_mode,
        ),
        refuse_below_min_volume=request.risk.refuse_below_min_volume,
    )


def _classify(exc: RiskError) -> tuple[str, str]:
    """Map a sizer failure onto a stable code, so the caller never parses prose."""
    message = str(exc)
    if "below the broker minimum" in message:
        return RejectionCode.BELOW_MIN_VOLUME, message
    if "cannot size a position" in message:
        return RejectionCode.BALANCE_NOT_POSITIVE, message
    if "costs nothing to lose" in message:
        return RejectionCode.ZERO_COST_PER_LOT, message
    if "same price" in message:
        return RejectionCode.NO_STOP, message
    if "is not protective" in message or "wrong side" in message:
        return RejectionCode.STOP_ON_WRONG_SIDE, message
    return RejectionCode.SPECIFICATION_UNUSABLE, message


def _plan_id(signal: TradeSignal) -> str:
    """A stable identifier for the plan, derived from the candle pair.

    Stable because it must be: the same pair of candles is the same trade, and the
    idempotency ledger in Phase 7 keys on it.
    """
    return signal.candle_id.replace("/", "-").replace("@", "-").replace(":", "-")


def _pct(value: Decimal) -> str:
    return f"{value:.4f}"
