"""The MetaTrader 5 broker, exercised against a fake terminal.

No terminal is required, and none is touched. What is being tested is the *translation and
the error handling*, which is where the dangerous mistakes live:

* exactly one ``order_send`` per placement, ever;
* the right MT5 order type for a pending stop;
* the identity tag surviving the terminal's 31-character comment field;
* an ambiguous retcode surfacing as ``ExecutionUnknownError`` rather than a retry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from stop_order_scalp.domain.enums import OrderKind
from stop_order_scalp.domain.exceptions import (
    BrokerNotConnectedError,
    BrokerRejectedError,
    ExecutionUnknownError,
    RetryableError,
)
from stop_order_scalp.domain.models import EnvironmentSettings, TradePlan
from stop_order_scalp.execution.mt5_broker import (
    MetaTrader5Broker,
    decode_comment,
    encode_comment,
)
from stop_order_scalp.market_data.mt5_module import MT5Module

EPOCH = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)


class FakeTerminal:
    """The slice of the terminal package the broker calls.

    Records every ``order_send`` so a test can assert that exactly one was made. That single
    assertion is the no-retry guarantee.
    """

    def __init__(self, *, connect_ok: bool = True) -> None:
        self.connect_ok = connect_ok
        self.initialized_with: dict[str, Any] | None = None
        self.shutdown_calls = 0
        self.sent: list[dict[str, Any]] = []
        self.send_retcode = 0
        self.order_rows: list[dict[str, Any]] = []
        self.position_rows: list[dict[str, Any]] = []
        self.last_error_pair: tuple[int, str] = (0, "")

    def initialize(self, **kwargs: Any) -> bool:
        self.initialized_with = kwargs
        if not self.connect_ok:
            self.last_error_pair = (-1, "terminal refused")
            return False
        return True

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def last_error(self) -> tuple[int, str]:
        return self.last_error_pair

    def symbol_info(self, name: str) -> Any:
        if name != "US30":
            return None
        return SimpleNamespace(
            name="US30",
            digits=1,
            point=Decimal("0.1"),
            trade_tick_size=Decimal("0.1"),
            trade_tick_value=Decimal("1.0"),
            trade_contract_size=Decimal("1.0"),
            volume_min=Decimal("0.1"),
            volume_max=Decimal("50.0"),
            volume_step=Decimal("0.1"),
            trade_stops_level=10,
            trade_freeze_level=0,
            currency="USD",
            leverage=100,
        )

    def symbol_info_tick(self, name: str) -> Any:
        del name
        return SimpleNamespace(time=EPOCH.timestamp(), bid=39999.0, ask=39999.5, last_volume=1)

    def symbol_select(self, name: str, select: bool) -> bool:
        del select
        return name == "US30"

    def account_info(self) -> Any:
        return SimpleNamespace(
            login=1234567,
            server="Alpari-Express-Demo",
            currency="USD",
            balance=10000.0,
            equity=10000.0,
            margin=0.0,
            margin_free=10000.0,
            leverage=100,
            trade_mode=0,
            trade_allowed=True,
        )

    def order_send(self, request: dict[str, Any]) -> Any:
        self.sent.append(dict(request))
        if self.send_retcode:
            return SimpleNamespace(
                retcode=self.send_retcode, order=0, comment="injected failure"
            )
        ticket = 7000 + len(self.sent)
        return SimpleNamespace(retcode=10008, order=ticket, comment="request placed")

    def orders_get(self, *, symbol: str | None = None, ticket: int | None = None) -> Any:
        rows = [
            r
            for r in self.order_rows
            if (symbol is None or r.get("symbol") == symbol)
            and (ticket is None or r.get("ticket") == ticket)
        ]
        return [SimpleNamespace(**r) for r in rows]

    def positions_get(self, *, symbol: str | None = None, ticket: int | None = None) -> Any:
        rows = [
            r
            for r in self.position_rows
            if (symbol is None or r.get("symbol") == symbol)
            and (ticket is None or r.get("ticket") == ticket)
        ]
        return [SimpleNamespace(**r) for r in rows]


@pytest.fixture
def terminal() -> FakeTerminal:
    return FakeTerminal()


@pytest.fixture
def live_settings() -> EnvironmentSettings:
    return EnvironmentSettings(
        environment="LIVE",  # type: ignore[arg-type]
        allow_live=True,
        allow_order=True,
        allow_close=True,
        mt5_login=1234567,
        mt5_server="Alpari-Express-Demo",
        mt5_timeout_ms=30000,
    )


@pytest.fixture
def broker(terminal: FakeTerminal, live_settings: EnvironmentSettings) -> MetaTrader5Broker:
    holder = MT5Module()
    holder._module = terminal  # type: ignore[assignment]
    instance = MetaTrader5Broker(holder, live_settings)
    instance.connect()
    return instance


def intent_for(plan: TradePlan) -> Any:
    from stop_order_scalp.domain.models import OrderIntent

    return OrderIntent.from_plan(plan, magic_number=20260930, deviation_points=10)


class TestConnection:
    def test_connect_passes_the_configured_credentials_when_a_password_is_set(
        self,
        terminal: FakeTerminal,
        live_settings: EnvironmentSettings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("SOS_MT5_PASSWORD", "not-a-real-password")
        holder = MT5Module()
        holder._module = terminal  # type: ignore[assignment]

        MetaTrader5Broker(holder, live_settings).connect()

        assert terminal.initialized_with is not None
        assert terminal.initialized_with["login"] == 1234567
        assert terminal.initialized_with["server"] == "Alpari-Express-Demo"
        assert terminal.initialized_with["timeout"] == 30000

    def test_without_a_password_it_attaches_and_never_logs_in(
        self,
        terminal: FakeTerminal,
        live_settings: EnvironmentSettings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A login with no password is answered with -2 and locked three demo accounts."""
        monkeypatch.delenv("SOS_MT5_PASSWORD", raising=False)
        holder = MT5Module()
        holder._module = terminal  # type: ignore[assignment]

        MetaTrader5Broker(holder, live_settings).connect()

        assert terminal.initialized_with is not None
        assert "login" not in terminal.initialized_with
        assert "password" not in terminal.initialized_with

    def test_a_refused_connection_raises(self, live_settings: EnvironmentSettings) -> None:
        holder = MT5Module()
        holder._module = FakeTerminal(connect_ok=False)  # type: ignore[assignment]

        with pytest.raises(BrokerNotConnectedError, match="refused to initialize"):
            MetaTrader5Broker(holder, live_settings).connect()

    def test_connect_is_idempotent(self, broker: MetaTrader5Broker, terminal: FakeTerminal) -> None:
        broker.connect()
        broker.connect()
        assert terminal.shutdown_calls == 0

    def test_using_a_disconnected_broker_refuses(
        self, terminal: FakeTerminal, live_settings: EnvironmentSettings
    ) -> None:
        holder = MT5Module()
        holder._module = terminal  # type: ignore[assignment]
        instance = MetaTrader5Broker(holder, live_settings)

        with pytest.raises(BrokerNotConnectedError, match="not connected"):
            instance.orders()

    def test_shutdown_is_idempotent(
        self, broker: MetaTrader5Broker, terminal: FakeTerminal
    ) -> None:
        """A second shutdown is a no-op, not a second call to the terminal."""
        broker.shutdown()
        broker.shutdown()
        assert terminal.shutdown_calls == 1
        assert not broker.is_connected


class TestReads:
    def test_server_time_comes_from_the_terminal(
        self, broker: MetaTrader5Broker
    ) -> None:
        """Broker server time decides candle closure, so it must not be the local clock."""
        assert broker.server_time() == EPOCH

    def test_account_is_converted(self, broker: MetaTrader5Broker) -> None:
        account = broker.account()
        assert account.login == 1234567
        assert account.currency == "USD"
        assert account.balance.amount == Decimal("10000.00")

    def test_an_unknown_symbol_is_reported(self, broker: MetaTrader5Broker) -> None:
        with pytest.raises(Exception, match="does not know a symbol"):
            broker.specification("NOSUCH")

    def test_the_specification_is_read_from_the_broker(self, broker: MetaTrader5Broker) -> None:
        spec = broker.specification("US30")
        assert spec.digits == 1
        assert spec.stops_level == 10


class TestPlaceOrder:
    def test_exactly_one_request_is_sent(
        self, broker: MetaTrader5Broker, terminal: FakeTerminal, plan: TradePlan
    ) -> None:
        broker.place_order(intent_for(plan))

        assert len(terminal.sent) == 1, "a placement must produce exactly one order_send"

    def test_a_buy_stop_uses_the_mt5_buy_stop_type(
        self, broker: MetaTrader5Broker, terminal: FakeTerminal, plan: TradePlan
    ) -> None:
        """ORDER_TYPE_BUY_STOP is 4. Getting this wrong places a different order entirely."""
        broker.place_order(intent_for(plan))

        assert terminal.sent[0]["type"] == 4
        assert terminal.sent[0]["action"] == 5, "TRADE_ACTION_PENDING is 5; 1 is TRADE_ACTION_DEAL"

    def test_the_request_carries_the_plans_numbers(
        self, broker: MetaTrader5Broker, terminal: FakeTerminal, plan: TradePlan
    ) -> None:
        broker.place_order(intent_for(plan))
        request = terminal.sent[0]

        assert request["symbol"] == "US30"
        assert request["volume"] == pytest.approx(float(plan.volume.lots))
        assert request["price"] == pytest.approx(float(plan.entry.value))
        assert request["sl"] == pytest.approx(float(plan.stop_loss.value))
        assert request["tp"] == pytest.approx(float(plan.take_profit.value))
        assert request["magic"] == 20260930


class TestNoRetry:
    """The property that makes this layer safe: one send, no matter how it fails."""

    @pytest.mark.parametrize("retcode", [10012, 10011, 10031, 10028, -1])
    def test_an_ambiguous_retcode_sends_exactly_once(
        self,
        broker: MetaTrader5Broker,
        terminal: FakeTerminal,
        plan: TradePlan,
        retcode: int,
    ) -> None:
        terminal.send_retcode = retcode

        with pytest.raises(ExecutionUnknownError):
            broker.place_order(intent_for(plan))

        assert len(terminal.sent) == 1, "an ambiguous outcome must not be resent"

    def test_a_timeout_is_not_retried(
        self, broker: MetaTrader5Broker, terminal: FakeTerminal, plan: TradePlan
    ) -> None:
        terminal.send_retcode = 10012

        with pytest.raises(ExecutionUnknownError):
            broker.place_order(intent_for(plan))

        assert len(terminal.sent) == 1

    def test_a_rejection_is_not_retried(
        self, broker: MetaTrader5Broker, terminal: FakeTerminal, plan: TradePlan
    ) -> None:
        terminal.send_retcode = 10016

        with pytest.raises(BrokerRejectedError):
            broker.place_order(intent_for(plan))

        assert len(terminal.sent) == 1

    def test_a_temporary_failure_raises_retryable_but_is_not_retried_here(
        self, broker: MetaTrader5Broker, terminal: FakeTerminal, plan: TradePlan
    ) -> None:
        """RetryableError is a *signal to the caller*, not licence for this layer to loop."""
        terminal.send_retcode = 10018

        with pytest.raises(RetryableError):
            broker.place_order(intent_for(plan))

        assert len(terminal.sent) == 1


class TestCommentEncoding:
    """The identity tag has to survive the terminal's 31-character comment field."""

    def test_a_short_comment_round_trips_exactly(self) -> None:
        tag = "0123456789abcdef0123"
        raw = encode_comment(tag, "M15 buy")

        assert decode_comment(raw) == (tag, "M15 buy")

    def test_only_ten_characters_of_prose_survive_a_twenty_character_tag(self) -> None:
        """A documented constraint, not an accident.

        The terminal allows 31 characters and the tag is 20 of them, so the human-readable
        half has about ten characters. Identity is worth more than prose, so the tag wins
        and the prose is cut -- but the budget is small enough to be worth stating.
        """
        raw = encode_comment("0123456789abcdef0123", "buy stop US30")

        assert len(raw) == 31
        assert decode_comment(raw)[0] == "0123456789abcdef0123"
        assert decode_comment(raw)[1] == "buy stop U"

    def test_a_long_comment_is_truncated_but_the_tag_survives(self) -> None:
        """Identity must never be the thing that gets cut."""
        tag = "0123456789abcdef0123"
        raw = encode_comment(tag, "x" * 200)

        assert len(raw) <= 31
        assert decode_comment(raw)[0] == tag

    def test_an_untagged_comment_decodes_to_an_empty_tag(self) -> None:
        assert decode_comment("some legacy comment") == ("", "some legacy comment")

    def test_the_tag_is_written_before_the_prose(self) -> None:
        assert encode_comment("tag123", "prose").startswith("tag123|")

    def test_the_sent_comment_carries_the_tag(
        self, broker: MetaTrader5Broker, terminal: FakeTerminal, plan: TradePlan
    ) -> None:
        """Whatever the tag is, it must be recoverable from what the terminal received.

        This is the property duplicate detection depends on: it reads the tag back off the
        book after a restart, so if the tag did not survive the send there would be nothing
        to match against.
        """
        intent = intent_for(plan)

        broker.place_order(intent)

        tag, _ = decode_comment(terminal.sent[0]["comment"])
        assert tag == intent.client_tag

    def test_a_read_order_decodes_its_tag(self, broker: MetaTrader5Broker) -> None:
        from stop_order_scalp.execution.mt5_broker import _order_from

        row = {
            "ticket": 99,
            "symbol": "US30",
            "type": 4,
            "volume_current": Decimal("0.4"),
            "price_open": Decimal("40000.0"),
            "sl": Decimal("39990.0"),
            "tp": Decimal("40020.0"),
            "magic": 20260930,
            "comment": "abc123def456abc123def4|buy stop",
            "time_setup": int(EPOCH.timestamp()),
            "time_expiration": 0,
        }

        record = _order_from(row, 1)

        assert record.client_tag == "abc123def456abc123def4"
        assert record.comment == "buy stop"
        assert record.kind is OrderKind.ORDER_KIND_BUY_STOP

    def test_a_stop_reported_as_zero_reads_as_absent(self) -> None:
        """MT5 uses 0 for "no stop set", which must not become a stop at price zero."""
        from stop_order_scalp.execution.mt5_broker import _order_from

        row = {
            "ticket": 99,
            "symbol": "US30",
            "type": 4,
            "volume_current": Decimal("0.4"),
            "price_open": Decimal("40000.0"),
            "sl": Decimal("0"),
            "tp": Decimal("40020.0"),
            "magic": 1,
            "comment": "t",
            "time_setup": int(EPOCH.timestamp()),
            "time_expiration": 0,
        }

        assert _order_from(row, 1).stop_loss is None


def _position_row(ticket: int, *, sl: float, tp: float) -> dict[str, Any]:
    return {
        "ticket": ticket,
        "symbol": "US30",
        "type": 0,
        "volume": 0.4,
        "price_open": 40000.0,
        "sl": sl,
        "tp": tp,
        "magic": 20260930,
        "comment": "t",
        "time": int(EPOCH.timestamp()),
        "profit": 0.0,
    }


class TestCancel:
    def test_cancelling_sends_a_request_and_reports_the_book(
        self, broker: MetaTrader5Broker, terminal: FakeTerminal
    ) -> None:
        assert broker.cancel_order(7001) is True
        assert len(terminal.sent) == 1
        assert terminal.sent[0]["order"] == 7001
        assert terminal.sent[0]["action"] == 8, "TRADE_ACTION_REMOVE is 8"

    def test_an_order_still_on_the_book_reports_not_cancelled(
        self, broker: MetaTrader5Broker, terminal: FakeTerminal
    ) -> None:
        """The book's answer, not the retcode's, decides the outcome."""
        terminal.order_rows = [
            {"ticket": 7001, "symbol": "US30", "magic": 1, "type": 4,
             "volume_current": Decimal("0.4"), "price_open": Decimal("40000.0"),
             "comment": "t", "time_setup": int(EPOCH.timestamp())}
        ]

        assert broker.cancel_order(7001) is False

    def test_an_ambiguous_cancel_response_is_resolved_by_reading_the_book(
        self, broker: MetaTrader5Broker, terminal: FakeTerminal
    ) -> None:
        """A cancellation whose outcome is unknown is a question, not a failure to retry."""
        terminal.send_retcode = 10012

        assert broker.cancel_order(7001) is True
        assert len(terminal.sent) == 1


class TestModifyPosition:
    def test_modifying_uses_the_sltp_action(
        self, broker: MetaTrader5Broker, terminal: FakeTerminal
    ) -> None:
        from stop_order_scalp.domain.value_objects import Price

        terminal.position_rows = [_position_row(42, sl=39990.0, tp=40100.0)]

        assert broker.modify_position(42, stop_loss=Price.parse("40005.0", 1)) is True
        assert terminal.sent[0]["action"] == 6, "TRADE_ACTION_SLTP is 6 and must not fill anything"
        assert terminal.sent[0]["position"] == 42

    def test_an_omitted_level_is_resent_at_its_current_value(
        self, broker: MetaTrader5Broker, terminal: FakeTerminal
    ) -> None:
        """Zero means *remove* to the terminal. Trailing sends only a stop; the target must
        survive it, or the first trailing step deletes the take-profit."""
        from stop_order_scalp.domain.value_objects import Price

        terminal.position_rows = [_position_row(42, sl=39990.0, tp=40100.0)]

        broker.modify_position(42, stop_loss=Price.parse("40005.0", 1))

        assert terminal.sent[0]["sl"] == pytest.approx(40005.0)
        assert terminal.sent[0]["tp"] == pytest.approx(40100.0), "the target must not be removed"

    def test_modifying_a_position_that_is_not_open_is_refused_without_sending(
        self, broker: MetaTrader5Broker, terminal: FakeTerminal
    ) -> None:
        with pytest.raises(BrokerRejectedError, match="no open position"):
            broker.modify_position(42, take_profit=None)

        assert terminal.sent == []

    def test_no_changes_is_success_for_a_trailing_stop_that_has_not_moved(
        self, broker: MetaTrader5Broker, terminal: FakeTerminal
    ) -> None:
        from stop_order_scalp.domain.value_objects import Price

        terminal.position_rows = [_position_row(42, sl=40005.0, tp=40100.0)]
        terminal.send_retcode = 10025

        assert broker.modify_position(42, stop_loss=Price.parse("40005.0", 1)) is True


class TestClassifyIsExercisedThroughTheBroker:
    def test_a_rejection_names_the_symbol(
        self, broker: MetaTrader5Broker, terminal: FakeTerminal, plan: TradePlan
    ) -> None:
        terminal.send_retcode = 10015

        with pytest.raises(BrokerRejectedError, match="US30"):
            broker.place_order(intent_for(plan))
