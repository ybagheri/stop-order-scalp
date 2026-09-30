# Operations

**Nothing here yet.** This directory is a placeholder so that the links in
[`README.md`](../../README.md) and [`ROADMAP.md`](../../ROADMAP.md) resolve.

| Document | Phase | Covers |
| --- | --- | --- |
| `LIVE_DEPLOYMENT.md` | 11 | The full `DRY_RUN` → `PAPER` → `DEMO` → `LIVE` progression, and the interlocks that guard each step |
| `DEMO_VALIDATION.md` | 11 | The read-only validation run against a demo terminal, with its actual findings |
| `RECOVERY.md` | 7 | What to do after a crash, a restart, or a send whose outcome is unknown |
| `TROUBLESHOOTING.md` | 10 | Reading the audit log, diagnosing a refusal |

## The progression, and what is real today

| Mode | Broker writes | Status |
| --- | --- | --- |
| `DRY_RUN` | none | Default. Runs the full pipeline against `SimulatedBroker` |
| `PAPER` | none | Follows live prices, sends nothing |
| `DEMO` | real, demo account | Phase 11 |
| `LIVE` | real | Not implemented, and gated three times over |

**`LIVE` is unreachable today** and there is no configuration that reaches it. When it
lands it will require, independently: `SOS_ENVIRONMENT=LIVE`, `SOS_ALLOW_LIVE=true`, and
`SOS_ALLOW_ORDER=true` — a separate opt-in per account-changing operation. The intent is
that no sequence of configuration mistakes gets there.

## Before you run anything

```bash
python -m stop_order_scalp validate-config
```

It prints the resolved value of every setting and names each file that contributed. A
silently-defaulted risk percentage is the failure mode this command exists to make
obvious, so read it rather than skimming it.
