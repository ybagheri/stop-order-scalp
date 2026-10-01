"""Monotonicity, proved as a property rather than on one example.

``docs/strategy/BASELINE.md`` §5 calls this out by name: a trailing stop that moves
backwards converts a winning trade into a losing one on spread alone. The specification
therefore asks for it to be *proved over generated price sequences*, including sequences
designed to tempt it -- and that is what this file does.

Three generators, each targeting a different way to break:

* :func:`price_paths` — arbitrary walks, the general case.
* :func:`adversarial_paths` — deliberately shaped so the stop *would* move backwards if the
  guard were wrong: a run-up followed by a sharp reversal.
* the constant, flat, and V-shaped paths that a walk generator produces rarely but which
  produce the clearest failures.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from itertools import pairwise

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from stop_order_scalp.domain.enums import Side
from stop_order_scalp.domain.models import PositionRecord, Tick
from stop_order_scalp.domain.value_objects import Price, SymbolSpecification, Volume
from stop_order_scalp.infrastructure.config import BreakEvenSettings, TrailingSettings
from stop_order_scalp.trailing.break_even import ConfiguredBreakEvenProvider
from stop_order_scalp.trailing.trailing_stop import TrailingStopProvider

from .conftest import tick

#: Hypothesis runs are bounded: the property is a proof over generated input, and an
#: unbounded search would dominate the suite's runtime without finding more.
PROPERTY_SETTINGS = settings(max_examples=200, deadline=None)


def _spec(stops_level: int = 0, freeze_level: int = 0) -> SymbolSpecification:
    """A permissive specification, so the property under test is the monotonicity guard and
    not the broker's distance limits.

    The limits get their own tests with tight values; here they are switched off precisely
    so a refusal cannot mask a backwards move.
    """
    return SymbolSpecification(
        name="US30",
        digits=1,
        point=Decimal("0.1"),
        tick_size=Decimal("0.1"),
        tick_value=Decimal("1.0"),
        contract_size=Decimal("1.0"),
        volume_min=Decimal("0.1"),
        volume_max=Decimal("50.0"),
        volume_step=Decimal("0.1"),
        stops_level=stops_level,
        freeze_level=freeze_level,
    )


SPEC = _spec()

#: Prices as integer tenths, which is the tick grid for this specification
#: (``point = 0.1``). Generating on the grid means the property is about monotonicity and
#: never about rounding.
#:
#: The magnitudes are worth stating: US30 around 40000.0 is **400000** tenths. A range of
#: 39_000 to 41_000 tenths would be prices of 3900.0, which is a different instrument and
#: would quietly test the wrong thing.
TENTHS = Decimal(10)

#: 39000.0 to 41000.0, in tenths.
prices = st.integers(min_value=390_000, max_value=410_000)

#: The entry every generated path starts from, in tenths.
ENTRY_TENTHS = 400_000

price_paths = st.lists(prices, min_size=2, max_size=40)


def adversarial_paths() -> st.SearchStrategy[tuple[Side, list[int]]]:
    """Sequences built to tempt the stop backwards.

    A run-up of 300 points followed by a reversal of up to 300 is the shape that makes
    ``bid - d`` fall below the current stop. Without the guard, that is exactly when a
    trailing BUY stop walks down through entry and out the other side.
    """
    sides = st.sampled_from([Side.SIDE_BUY, Side.SIDE_SELL])

    @st.composite
    def build(draw: st.DrawFn) -> tuple[Side, list[int]]:
        side = draw(sides)
        # Movements in tenths: 4000 tenths is 400.0 price units, i.e. 4000 points.
        up = draw(st.integers(min_value=0, max_value=4_000))
        down = draw(st.integers(min_value=0, max_value=4_000))
        peak = ENTRY_TENTHS + up
        path = [ENTRY_TENTHS]
        # Rise to the peak, then reverse.
        path.extend(range(ENTRY_TENTHS, peak + 1, 100))
        path.extend(range(peak, peak - down - 1, -100))
        return side, path

    return build()


def _position(
    side: Side, entry: Decimal, stop: Decimal | None, moment: datetime
) -> PositionRecord:
    return PositionRecord(
        ticket=1,
        symbol="US30",
        side=side,
        volume=Volume.of(Decimal("0.4")),
        entry=Price(entry, 1),
        stop_loss=Price(stop, 1) if stop is not None else None,
        take_profit=None,
        magic_number=20260930,
        comment="",
        opened_at=moment,
        client_tag="t",
    )


def _observation(bid: Decimal, moment: datetime) -> Tick:
    """A one-tick spread.

    Built on the grid rather than with the ``conftest.tick`` helper, because these paths
    carry prices as integer tenths already and re-parsing them would lose that.
    """
    return Tick(moment=moment, bid=Price(bid, 1), ask=Price(bid + Decimal("0.1"), 1))


def _apply_sequence(
    side: Side, path: list[int], specification: SymbolSpecification = SPEC
) -> list[Decimal]:
    """Run a price path through both rules and return every stop that was ever in place.

    This is the model's state: the stop in place after each tick, with each proposal applied
    exactly as the position manager would apply it. The assertion is then over this whole
    sequence rather than over any single pair.

    ``path`` holds prices as integer tenths, and the first entry is the entry price. The
    returned list always has at least one element and never contains ``None``: the
    position starts with a stop 100 points from entry, which is the strategy's own shape.
    """
    provider = TrailingStopProvider(
        TrailingSettings(distance_points=100, min_step_points=1)
    )
    break_even = ConfiguredBreakEvenProvider(BreakEvenSettings(trigger_points=50))
    moment = tick("40000.0").moment
    entry = Decimal(path[0]) / TENTHS
    direction = 1 if side is Side.SIDE_BUY else -1
    current_stop: Decimal = entry - Decimal(10) * direction
    seen: list[Decimal] = [current_stop]

    for raw in path[1:]:
        market = Decimal(raw) / TENTHS
        live = _position(side, entry, current_stop, moment)
        observation = _observation(market, moment)

        # Mirrors PositionManager._decide: break-even wins outright, and trailing is only
        # consulted when break-even proposed nothing.
        #
        # This ordering is load-bearing, not cosmetic. Both proposals are computed against
        # the *same* position, so applying both in one tick means the second is derived from
        # a stop that the first has already moved -- and for a SELL whose stop has just been
        # pulled down to entry, a trailing level computed from the old stop lands *above* it.
        # Applying both would move the stop backwards on the same tick. A property that fed
        # both proposals in was the first thing to expose this.
        proposal = break_even.evaluate(live, observation, specification).proposal
        if proposal is None:
            proposal = provider.evaluate(live, observation, specification).proposal
        if proposal is None:
            continue

        assert proposal.is_monotonic(), (
            f"{side} stop moved backwards to {proposal.proposed_stop} from {current_stop}"
        )
        current_stop = proposal.proposed_stop.value
        seen.append(current_stop)
    return seen


class TestBuyStopsOnlyRise:
    @PROPERTY_SETTINGS
    @given(path=price_paths)
    def test_over_generated_paths(self, path: list[int]) -> None:
        stops = _apply_sequence(Side.SIDE_BUY, path)

        for older, newer in pairwise(stops):
            assert newer >= older, "a BUY stop loss may only move up"

    @PROPERTY_SETTINGS
    @given(pair=adversarial_paths())
    def test_over_adversarial_paths(self, pair: tuple[Side, list[int]]) -> None:
        side, path = pair
        stops = _apply_sequence(side, path)

        for older, newer in pairwise(stops):
            if side is Side.SIDE_BUY:
                assert newer >= older
            else:
                assert newer <= older


class TestSellStopsOnlyFall:
    @PROPERTY_SETTINGS
    @given(path=price_paths)
    def test_over_generated_paths(self, path: list[int]) -> None:
        stops = _apply_sequence(Side.SIDE_SELL, path)

        for older, newer in pairwise(stops):
            assert newer <= older, "a SELL stop loss may only move down"


class TestAdversarialReversal:
    """The specific case the specification warns about, stated as a test."""

    def test_a_reversal_after_a_run_up_leaves_the_stop_where_it_was(self) -> None:
        """Price rises 300 points, then falls 300. The stop must not follow it back down.

        Prices are in tenths, so 400000 is 40000.0 and each 100 tenths is 10 price units.
        """
        path = [
            ENTRY_TENTHS,
            ENTRY_TENTHS + 2_000,
            ENTRY_TENTHS + 3_000,
            ENTRY_TENTHS + 2_000,
            ENTRY_TENTHS + 1_000,
            ENTRY_TENTHS,
            ENTRY_TENTHS - 1_000,
        ]

        stops = _apply_sequence(Side.SIDE_BUY, path)

        for older, newer in pairwise(stops):
            assert newer >= older
        # The stop did move up during the run-up, so the test is not vacuous.
        assert max(stops) > Decimal("39990.0")

    def test_a_v_shaped_path_never_lowers_a_buy_stop(self) -> None:
        path = [
            ENTRY_TENTHS,
            ENTRY_TENTHS + 5_000,
            ENTRY_TENTHS + 1_000,
            ENTRY_TENTHS + 500,
            ENTRY_TENTHS - 1_000,
        ]

        stops = _apply_sequence(Side.SIDE_BUY, path)

        assert stops == sorted(stops)

    def test_a_flat_path_produces_no_moves_at_all(self) -> None:
        """A constant price must not send a request per tick."""
        stops = _apply_sequence(Side.SIDE_BUY, [ENTRY_TENTHS] * 10)

        assert len(stops) == 1


class TestNoTickCanSendTwoRequests:
    @PROPERTY_SETTINGS
    @given(path=price_paths)
    def test_a_stop_is_never_proposed_twice_for_the_same_level(self, path: list[int]) -> None:
        """Idempotency as a property: a repeated level is refused, not re-sent.

        The stop is advanced as each proposal is accepted, exactly as the position manager
        would. Without that, the same level is legitimately proposed on every tick and the
        test would be measuring the harness rather than the provider.
        """
        provider = TrailingStopProvider(TrailingSettings(distance_points=100, min_step_points=1))
        spec = _spec()
        moment = tick("40000.0").moment
        entry = Decimal(path[0]) / TENTHS
        current = entry - Decimal(10)
        sent: list[Decimal] = []

        for raw in path:
            market = Decimal(raw) / TENTHS
            live = _position(Side.SIDE_BUY, entry, current, moment)
            decision = provider.evaluate(live, _observation(market, moment), spec)
            if decision.proposal is not None:
                level = decision.proposal.proposed_stop.value
                assert level not in sent, "the same stop level was proposed twice"
                sent.append(level)
                current = level


class TestManagedStopAgrees:
    """``ManagedStop.is_monotonic`` and the providers must never disagree.

    Two implementations of the same invariant is one too many, so the provider's answer is
    checked against the domain object's own answer on every proposal.
    """

    @PROPERTY_SETTINGS
    @given(
        side=st.sampled_from([Side.SIDE_BUY, Side.SIDE_SELL]),
        current=st.decimals(min_value=Decimal("39000"), max_value=Decimal("41000")),
        proposed=st.decimals(min_value=Decimal("39000"), max_value=Decimal("41000")),
    )
    def test_the_helper_matches_the_domain_rule(
        self, side: Side, current: Decimal, proposed: Decimal
    ) -> None:
        from stop_order_scalp.domain.models import ManagedStop
        from stop_order_scalp.execution.position_manager import monotonic_ok

        managed = ManagedStop(
            position_ticket=1,
            side=side,
            current_stop=Price(current, 1),
            proposed_stop=Price(proposed, 1),
            reason="test",
            distance_points=Decimal(0),
        )
        assert monotonic_ok(
            Price(current, 1), Price(proposed, 1), side
        ) == managed.is_monotonic()


@pytest.mark.parametrize(
    ("side", "current", "proposed", "monotonic"),
    [
        # A BUY stop may rise and may not fall.
        (Side.SIDE_BUY, "39990.0", "40000.0", True),
        (Side.SIDE_BUY, "40000.0", "39990.0", False),
        # A SELL stop may fall and may not rise.
        (Side.SIDE_SELL, "40010.0", "40000.0", True),
        (Side.SIDE_SELL, "40000.0", "40010.0", False),
    ],
)
def test_the_direction_rule_on_both_sides(
    side: Side, current: str, proposed: str, monotonic: bool
) -> None:
    """The direction check itself, stated as a table in both directions.

    A one-sided test would pass with the sign inverted, which is exactly the kind of bug the
    monotonicity guard exists to prevent.
    """
    from stop_order_scalp.domain.models import ManagedStop

    managed = ManagedStop(
        position_ticket=1,
        side=side,
        current_stop=Price.parse(current, 1),
        proposed_stop=Price.parse(proposed, 1),
        reason="test",
        distance_points=Decimal(0),
    )

    assert managed.is_monotonic() is monotonic


def test_a_position_with_no_current_stop_is_always_monotonic() -> None:
    """Nothing to move backwards from."""
    from stop_order_scalp.domain.models import ManagedStop

    managed = ManagedStop(
        position_ticket=1,
        side=Side.SIDE_BUY,
        current_stop=None,
        proposed_stop=Price.parse("40000.0", 1),
        reason="test",
        distance_points=Decimal(0),
    )

    assert managed.is_monotonic()
