# Research

**Nothing here yet.** This directory is a placeholder so that the links in
[`README.md`](../../README.md) and [`ROADMAP.md`](../../ROADMAP.md) resolve.

The research and optimization layer lands in **Phase 12** and will produce:

| Document | Covers |
| --- | --- |
| `README.md` (this file, expanded) | How to run a sweep, and how to read the result honestly |
| `FINDINGS.md` | What was tried, what the outcome was, **including negative results** |

## The rules this layer operates under

* **Disabled by default.** `filters_enabled: false` in `config/default.yaml`. The baseline
  strategy must remain reproducible from that file alone, with the research layer
  contributing nothing.
* **It may not modify the baseline.** Any filter or parameter change is additive,
  configurable, independently testable, and off unless explicitly enabled.
* **Negative results get written down.** A sweep that finds nothing is a result, and
  recording it is the only thing that stops the same sweep being run again in six months.
* **It does not touch live execution.** Research runs against the backtest engine and the
  `SimulatedBroker`; live execution and simulation stay fully separate code paths.

## The honesty requirement

Optimization results are the easiest place in any trading project to fool yourself. A
parameter sweep that finds a "best" setting on the data it was tuned on has found
overfitting, not an edge. The reporting in Phase 9 therefore splits train / validation /
out-of-sample and supports walk-forward, precisely so that Phase 12 has something honest
to report against.

**No profitability is claimed anywhere in this repository, and none will be until a
backtest has been run on real data and reported here.**
