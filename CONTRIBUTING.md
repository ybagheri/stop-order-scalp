# Contributing

Thank you. This document defines what a change has to satisfy before it is considered
finished.

## Before you start

Read, in this order:

1. [`HANDOFF.md`](HANDOFF.md) — current state, decisions, remaining work
2. [`ROADMAP.md`](ROADMAP.md) — the phase you are working on
3. [`README.md`](README.md) — how the system is used
4. [`docs/architecture/PHASE0_AUDIT.md`](docs/architecture/PHASE0_AUDIT.md) — why it is
   built this way

## The one rule that overrides everything

**The baseline strategy must not change.** It is:

* US30 only
* M15 (last **closed** candle) decides direction
* BUY STOP at `M1.high + 10 points`; SELL STOP at `M1.low − 10 points`
* 0.5 % of balance risk, `$6/lot` commission
* TP 1000 points, 1:1 R:R available
* Configurable break-even trigger, then a 100-point monotonic trailing stop
* A new pending order immediately after a position closes

Any improvement must be **configurable**, **documented**, **independently testable** and
**disabled by default**. See [`ROADMAP.md`](ROADMAP.md) Phase 12.

## Setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
copy .env.example .env
```

Optional extras:

| Extra | Purpose |
| --- | --- |
| `[mt5]` | the `MetaTrader5` Python package, needed for anything but dry-run |
| `[albrooks]` | the Al Brooks Price Action Engine, for Phase 8 |
| `[backtest]` | analysis dependencies for Phase 9 |
| `[dev]` | pytest, ruff, mypy, hypothesis |

## Quality gates — all four must pass

```bash
python -m ruff check .
python -m mypy src tests
python -m pytest
python scripts/check_architecture.py
```

A change is not finished until all four are green.

### Architecture gate

`scripts/check_architecture.py` fails the build if:

* `MetaTrader5` is imported anywhere except `execution/mt5_broker.py` and
  `market_data/mt5_feed.py`
* `MetaTrader5` is imported at module scope rather than lazily inside a function
* `albrooks` is imported anywhere except `integrations/al_brooks_adapter.py`
* a layer boundary is violated (e.g. `domain/` importing from `execution/`)
* a `float` is used for money, price or volume in the domain, risk or trailing layers

Do not add `# noqa` to silence these. Fix the import.

## Style

* Python `>=3.11`. Use `StrEnum`, `X | None`, `from __future__ import annotations` only
  where genuinely needed.
* `Decimal` for money, price and volume. `float` only at the MetaTrader 5 boundary.
* Timezone-aware UTC datetimes only. A naive `datetime` is a bug.
* Type annotations on every public function and class. `mypy --strict` is on.
* Line length 100. `ruff` handles formatting-adjacent lint.
* No global mutable state.
* No comments that merely restate the code. Comments explain **why**.
* Docstrings on public classes and functions state behaviour, invariants and failure modes.

## Testing

* Unit tests never touch MetaTrader 5, the network, or the filesystem outside `tmp_path`.
* Isolation is by **construction**: inject a fake that satisfies a `Protocol`. Do not
  `mock.patch` the terminal.
* Property-based tests (`hypothesis`) are required for anything monotonic, rounding
  related, or order-lifecycle related. See the spec's §31 and §32.
* Time is injected. Never call `time.time()` or `datetime.now()` in testable code.
* Deterministic ordering: no reliance on dict iteration order for output, no wall clock.
* A test that cannot fail is not a test. Prefer assertions on behaviour over assertions
  on log strings.

## Secrets

* `.env` holds credentials. It is git-ignored. **Never commit it.**
* `config/default.yaml` holds strategy parameters. **Never put a secret in it.**
* The audit log model has no secret field, so there is nothing to redact. Do not add one
  and then "remember to filter it" — pass `bool(x)`, never `x`.

## Commit messages

Conventional Commits, imperative mood, scoped:

```
feat(strategy): implement M15 direction and M1 stop entries
fix(risk): round volume down to the broker volume step
test(trailing): property test for BUY monotonicity
docs(architecture): record the Decimal decision
refactor(execution): extract order validation from the manager
chore(deps): pin ruff 0.16.8
```

No meaningless commits. Do not commit and then immediately amend to fix the message.

## Pull request checklist

- [ ] `ruff`, `mypy`, `pytest`, `check_architecture.py` all green
- [ ] New behaviour has tests, including the failure path
- [ ] Documentation updated — `docs/` first, then `README.md` **and** `README.fa.md`
- [ ] `ROADMAP.md`, `HANDOFF.md`, `CHANGELOG.md` updated
- [ ] `git status` clean, no secrets in the diff
- [ ] Baseline strategy unchanged, or the change is opt-in and defaults to off

## Reporting a bug

Include: `SOS_ENVIRONMENT`, Python version, MetaTrader 5 build, broker, symbol name, the
exact `SOS_*` settings **with credentials redacted**, and the relevant log excerpt. The
structured logs carry enough detail to reconstruct most reports.