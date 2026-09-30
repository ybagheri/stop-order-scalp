# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Phase 0 — Repository Audit

#### Added

- `docs/architecture/PHASE0_AUDIT.md` — audit of `stop-order-scalp`, `auto-trade` and
  `al-brooks-price-action-engine`; reuse analysis with explicit justification for every
  adoption and rejection; key architectural decisions; implementation plan.
- `README.md` and `README.fa.md` — bilingual documentation entry points.
- `ROADMAP.md` — 13 phases (0–12) with per-phase deliverables and quality gates.
- `HANDOFF.md` — mandatory state file for cross-laptop / cross-agent continuity.

#### Decisions

- Python application architecture with the **native MetaTrader 5 Python API** for
  execution. MQL5 and GUI automation rejected for now, with the conditions under which
  each would be reconsidered documented.
- Domain layer is MT5-free; the boundary is enforced by a CI script.
- `Decimal` for all money, price and volume; point↔price conversion always goes through
  the broker's `SymbolSpecification`.
- Order intent is persisted **before** the send; an unknown send outcome is never
  retried, only re-observed.
- The broker's live state is authoritative on restart.
- Secrets are structurally unloggable — no secret field exists on the log event model.

#### Notes

- No code exists yet. No tests have been run. No profitability claim is made.