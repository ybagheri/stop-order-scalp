"""The project must never attempt a login it cannot complete.

How three demo accounts got locked
----------------------------------
Before Phase 11's fix, `MT5Feed.connect` passed login, password and server to
`mt5.initialize` **unconditionally**. With `SOS_MT5_PASSWORD` unset that meant:

    mt5.initialize(path=..., login=53137121, password=None, server="Alpari-MT5-Demo")

MetaTrader 5 does not read that as "attach to the session you already have". It reads it as a
login attempt with an invalid password and answers:

    (-2, 'Invalid "password" argument')

Running that repeatedly is what a broker counts as failed logins, and it is what locked three
demo accounts in a row. No password was ever *entered* -- but an invalid one was *sent*, and
the distinction is the whole bug. A login attempt without a password is worse than no login
attempt at all.

The rule this file pins
-----------------------
**Credentials go out only when a password exists to send.** Otherwise the connection attaches to
whatever session the terminal already has, which is both safer and the normal case for an
installed, signed-in terminal.

An empty string is not "no password". `if password:` treats `""` as absent, and the
configuration loader reduces a missing value to `None`. Both spellings must therefore fail
this test, because both would otherwise produce the same rejected login.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stop_order_scalp.domain.enums import Environment
from stop_order_scalp.domain.models import EnvironmentSettings
from stop_order_scalp.market_data.mt5_feed import MT5Feed
from stop_order_scalp.market_data.mt5_module import MT5Module


class RecordingApi:
    """Captures exactly what `connect` sends to MetaTrader 5."""

    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def initialize(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        return self.result

    def shutdown(self) -> None:
        return None

    def version(self) -> tuple[int, ...]:
        return (5, 0, 0)


class FakeModule(MT5Module):
    """The MT5 seam, so no terminal is needed to assert what would be sent.

    A real subclass rather than a duck type, so the type checker sees the same thing the
    constructor expects -- and so a change to ``MT5Module``'s surface breaks this loudly
    instead of passing a shape that no longer matches.
    """

    def __init__(self, api: RecordingApi) -> None:
        self._api = api

    def api(self) -> Any:
        return self._api

    def describe_last_error(self) -> tuple[int, str]:
        return (-2, 'Invalid "password" argument')


def settings(**overrides: Any) -> EnvironmentSettings:
    base: dict[str, Any] = {
        "environment": Environment.DRY_RUN,
        "allow_live": False,
        "allow_order": False,
        "mt5_path": r"C:\somewhere\terminal64.exe",
        "mt5_login": 53137121,
        "mt5_server": "Alpari-MT5-Demo",
        "mt5_timeout_ms": 60000,
    }
    base.update(overrides)
    return EnvironmentSettings(**base)


@pytest.fixture
def no_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """The state that produced three locked accounts: a login, and no password."""
    monkeypatch.delenv("SOS_MT5_PASSWORD", raising=False)


class TestNoLoginWithoutAPassword:
    def test_a_login_is_not_sent_when_there_is_no_password(
        self, no_password: None
    ) -> None:
        api = RecordingApi()
        MT5Feed(FakeModule(api)).connect(settings())

        assert api.calls, "nothing was sent, so the test is not exercising the path"
        sent = api.calls[0]
        assert "login" not in sent, (
            f"a login was sent with no password: {sent}. MetaTrader 5 treats this as a login "
            "attempt with an invalid password and rejects it, and repeating it locks the "
            "account."
        )
        assert "password" not in sent
        assert "server" not in sent

    def test_an_empty_password_is_treated_as_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`""` is not "no password" to MetaTrader 5, and must not be forwarded as one."""
        monkeypatch.setenv("SOS_MT5_PASSWORD", "")
        api = RecordingApi()
        MT5Feed(FakeModule(api)).connect(settings())

        sent = api.calls[0]
        assert "password" not in sent, f"an empty password was forwarded: {sent}"
        assert "login" not in sent

    def test_only_the_path_goes_out(self, no_password: None) -> None:
        """Stated as an exact shape, so an added argument cannot slip through unnoticed."""
        api = RecordingApi()
        MT5Feed(FakeModule(api)).connect(settings())

        sent = api.calls[0]
        assert set(sent) <= {"path", "timeout", "portable"}, sent
        assert sent["path"] == r"C:\somewhere\terminal64.exe"

    def test_the_path_alone_is_still_passed(self, no_password: None) -> None:
        """Attaching to the existing session needs the path, or nothing happens at all.

        Pinned together with the shape assertion rather than alone: as a standalone test this
        one passed against the broken code, because a path *was* present in both shapes. The
        property worth stating is the combination -- the path goes out **and** the credentials
        do not.
        """
        api = RecordingApi()
        MT5Feed(FakeModule(api)).connect(settings())

        sent = api.calls[0]
        assert sent.get("path") == r"C:\somewhere\terminal64.exe"
        assert "login" not in sent
        assert "password" not in sent
        assert "server" not in sent


class TestWithAPasswordConfigured:
    def test_credentials_are_sent_when_a_password_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Opt-in still works, for an unattended login to a terminal nobody signed into."""
        monkeypatch.setenv("SOS_MT5_PASSWORD", "a-real-password")
        api = RecordingApi()
        MT5Feed(FakeModule(api)).connect(settings())

        sent = api.calls[0]
        assert sent["login"] == 53137121
        assert sent["password"] == "a-real-password"
        assert sent["server"] == "Alpari-MT5-Demo"

    def test_the_password_is_never_written_anywhere(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read on demand from the environment, never copied into a field.

        A credential held on a long-lived object is one refactor away from being logged. Read
        at the moment of use and discarded immediately is the only shape that cannot leak by
        accident, so the absence of a stored field is asserted rather than assumed.
        """
        monkeypatch.setenv("SOS_MT5_PASSWORD", "a-real-password")
        api = RecordingApi()
        feed = MT5Feed(FakeModule(api))
        feed.connect(settings())

        assert "a-real-password" not in repr(feed)

        # The feed uses __slots__, so it has no __dict__ at all -- which is a stronger
        # guarantee than "no attribute happens to hold it": there is nowhere for a credential
        # to be stashed, and adding one would require declaring the slot first.
        assert not hasattr(feed, "__dict__"), (
            "the feed has a __dict__, so anything could be attached to it at runtime"
        )
        declared = set(getattr(type(feed), "__slots__", ()))
        assert not any("password" in name.lower() or "secret" in name.lower() for name in declared), (
            f"the feed declares a slot that could hold a credential: {sorted(declared)}"
        )


class TestTheConfigurationCannotHoldAPassword:
    def test_environment_settings_has_no_password_field(self) -> None:
        """Structural, so no future change can quietly add one.

        A broker credential in an object that gets logged, serialised or put in a diagnostics
        bundle is a leak with a long tail. The field is absent by design, and the absence is
        asserted so a "harmless" addition is a failing test.
        """
        fields = set(EnvironmentSettings.__dataclass_fields__)
        assert "mt5_password" not in fields
        assert "password" not in fields
        assert fields  # sanity: the dataclass does have fields

    def test_the_project_ships_no_password_anywhere(self) -> None:
        """A grep, as a test, because a leaked credential is not a thing to find by review."""
        offenders = []
        for path in Path("src").rglob("*.py"):
            body = path.read_text(encoding="utf-8")
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith(("#", '"""', "'''")):
                    continue
                if "password" not in stripped.lower():
                    continue
                # Reading the variable is fine; assigning a literal is not.
                if any(
                    marker in stripped
                    for marker in ('password = "', "password='", 'password="')
                ):
                    offenders.append(f"{path}: {stripped[:70]}")
        assert not offenders, f"a hard-coded password literal: {offenders}"
