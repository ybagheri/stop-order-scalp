"""The protocols in :mod:`stop_order_scalp.domain.interfaces`, and importability.

These are the abstractions the whole system is built on, and until this module existed
nothing tested them. That is not a hypothetical risk: ``Broker`` declared ``Protocol`` as
its *first* base alongside three protocols that already inherited from it, which is an
inconsistent MRO. The class could not be created, so importing
``stop_order_scalp.domain.interfaces`` raised ``TypeError`` -- and the 212-test suite
stayed green, because nothing imported it.

The first two test classes are therefore the important ones: they make "every module
imports" and "every ``__all__`` entry resolves" standing invariants of the suite.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

import pytest

import stop_order_scalp
from stop_order_scalp.domain import interfaces
from stop_order_scalp.domain.interfaces import (
    AccountReader,
    AuditSink,
    Broker,
    Clock,
    MarketDataProvider,
    OrderBookReader,
    OrderExecutor,
    PositionManagerBroker,
)

# =============================================================================
# Standing invariants
# =============================================================================


def _all_module_names() -> list[str]:
    names = [stop_order_scalp.__name__]
    names.extend(
        module.name for module in pkgutil.walk_packages(stop_order_scalp.__path__, "stop_order_scalp.")
    )
    return sorted(names)


class TestEveryModuleImports:
    def test_the_package_walks_to_a_non_trivial_number_of_modules(self) -> None:
        # Guards the test below: an empty walk would make it vacuous.
        assert len(_all_module_names()) > 15

    @pytest.mark.parametrize("name", _all_module_names())
    def test_module_imports(self, name: str) -> None:
        # Regression: an inconsistent MRO among protocol bases is a TypeError raised at
        # import time, and nothing else in the suite would notice.
        assert importlib.import_module(name) is not None


class TestEveryDeclaredExportResolves:
    @pytest.mark.parametrize("name", _all_module_names())
    def test_all_entries_exist(self, name: str) -> None:
        # Regression: config.__all__ exported a "PathSettings" that was never defined, so
        # `from ... import *` raised AttributeError.
        module = importlib.import_module(name)
        missing = [symbol for symbol in getattr(module, "__all__", []) if not hasattr(module, symbol)]
        assert missing == []


# =============================================================================
# The protocols exist and are usable
# =============================================================================


class TestProtocolSurface:
    @pytest.mark.parametrize(
        "protocol",
        [Clock, MarketDataProvider, Broker, AuditSink],
        ids=lambda p: p.__name__,
    )
    def test_the_four_required_protocols_are_declared(self, protocol: type) -> None:
        assert protocol is not None

    def test_the_narrow_protocols_are_declared(self) -> None:
        for protocol in (
            AccountReader,
            OrderBookReader,
            OrderExecutor,
            PositionManagerBroker,
        ):
            assert protocol is not None

    def test_broker_inherits_every_narrow_protocol(self) -> None:
        # The point of the composition: anything accepting a narrow protocol must also
        # accept a Broker, and that has to be true structurally, not by convention.
        for protocol in (AccountReader, OrderBookReader, OrderExecutor):
            for name in vars(protocol):
                if name.startswith("_"):
                    continue
                assert hasattr(Broker, name), f"Broker is missing {protocol.__name__}.{name}"

    def test_broker_declares_protocol_last(self) -> None:
        # A Protocol base listed before another Protocol base is an inconsistent MRO and
        # raises TypeError at class-creation time. Pin the ordering that works.
        bases = Broker.__bases__
        assert bases[-1].__name__ == "Protocol"
        assert sum(1 for base in bases if base.__name__ == "Protocol") == 1


# =============================================================================
# Structural typing: a hand-written fake satisfies the protocol
# =============================================================================


class FakeClock:
    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self) -> datetime:
        return self._moment

    def sleep(self, seconds: float) -> None:
        del seconds


class FakeAuditSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def flush(self) -> None:
        return None


class FakeBroker:
    """A minimal venue. Satisfies Broker structurally, with no inheritance at all."""

    def __init__(self) -> None:
        self._connected = False

    def connect(self) -> None:
        self._connected = True

    def shutdown(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def account(self) -> Any:
        return None

    def specification(self, symbol: str) -> Any:
        del symbol
        raise NotImplementedError

    def symbol_available(self, symbol: str) -> bool:
        del symbol
        return True

    def server_time(self) -> datetime:
        return datetime(2026, 3, 12, 12, 30, tzinfo=UTC)

    def positions(self, **kwargs: Any) -> Sequence[Any]:
        del kwargs
        return ()

    def orders(self, **kwargs: Any) -> Sequence[Any]:
        del kwargs
        return ()

    def position_by_ticket(self, ticket: int) -> Any:
        del ticket
        return None

    def place_order(self, intent: Any) -> Any:
        del intent
        raise NotImplementedError

    def cancel_order(self, ticket: int) -> bool:
        del ticket
        return True

    def modify_position(self, ticket: int, **kwargs: Any) -> bool:
        del ticket, kwargs
        return True

    def stream_ticks(self, symbol: str) -> Iterator[Any]:
        del symbol
        return iter(())

    def leverage_for(self, symbol: str) -> Decimal:
        del symbol
        return Decimal(100)


class TestStructuralTyping:
    def test_a_fake_clock_satisfies_the_clock_protocol(self) -> None:
        clock: Clock = FakeClock(datetime(2026, 3, 12, 12, 30, tzinfo=UTC))
        assert clock.now() == datetime(2026, 3, 12, 12, 30, tzinfo=UTC)

    def test_a_fake_sink_satisfies_the_audit_sink_protocol(self) -> None:
        sink = FakeAuditSink()
        typed: AuditSink = sink
        typed.emit({"event": "test"})
        typed.flush()
        # Asserted through the concrete fake, because the protocol deliberately exposes
        # no way to read back what was recorded.
        assert sink.events == [{"event": "test"}]

    def test_a_fake_broker_satisfies_the_broker_protocol(self) -> None:
        broker: Broker = FakeBroker()
        broker.connect()
        assert broker.is_connected is True
        assert broker.symbol_available("US30") is True
        assert broker.server_time() == datetime(2026, 3, 12, 12, 30, tzinfo=UTC)
        assert broker.orders() == ()
        assert broker.leverage_for("US30") == Decimal(100)
        broker.shutdown()
        assert broker.is_connected is False

    def test_the_fake_needs_no_inheritance(self) -> None:
        # If FakeBroker had to inherit Broker to typecheck, the protocol would not be
        # buying anything over an abstract base class.
        assert not issubclass(FakeBroker, interfaces.AuditSink)
        assert Broker not in FakeBroker.__mro__


class TestRuntimeCheckability:
    def test_isinstance_works_against_a_structural_fake(self) -> None:
        # @runtime_checkable allows isinstance for a structurally compatible object.
        assert isinstance(FakeBroker(), Broker)

    def test_isinstance_rejects_an_incomplete_implementation(self) -> None:
        class NotABroker:
            def connect(self) -> None: ...

        assert not isinstance(NotABroker(), Broker)

    def test_issubclass_is_unavailable_because_broker_has_a_property(self) -> None:
        # A protocol with any non-method member -- Broker.is_connected is a property --
        # cannot support issubclass(), and Python says so rather than answering wrongly.
        # Annotate against the protocol; do not branch on issubclass at run time.
        #
        # mypy already rejects this statically, which is the desired outcome and the reason
        # the ignore is safe: the test exists to pin the runtime contract for callers who
        # are not type-checked.
        with pytest.raises(TypeError, match="non-method members"):
            issubclass(FakeBroker, Broker)  # type: ignore[misc]

    def test_a_methods_only_runtime_checkable_protocol_does_support_issubclass(self) -> None:
        @runtime_checkable
        class Tiny(Protocol):
            def ping(self) -> None: ...

        class HasPing:
            def ping(self) -> None: ...

        assert issubclass(HasPing, Tiny)
