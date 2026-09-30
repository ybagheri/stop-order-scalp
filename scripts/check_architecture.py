#!/usr/bin/env python
"""Architecture boundary enforcement.

A dependency rule that lives in a document is a rule that erodes. This script turns the
rules that matter into a build failure, so a violation is caught by CI rather than by
whoever reviews the diff six months later.

Checks, in order:

1. ``MetaTrader5`` may be imported only by ``market_data/mt5_feed.py`` and
   ``execution/mt5_broker.py``, and only inside a function body (lazy import), never at
   module scope.
2. ``albrooks`` may be imported only by ``integrations/al_brooks_adapter.py``.
3. Layer direction: ``domain`` imports nothing from the project but itself; ``strategy``,
   ``risk``, ``trailing`` and ``lifecycle`` may not import ``execution``,
   ``application``, ``cli``, ``integrations``, ``backtest`` or ``research``;
   ``market_data`` may not import ``strategy``/``risk``/``execution``/``lifecycle``.
4. No module outside ``execution`` and ``market_data`` may reference a bare ``float``
   cast of money: the ban is on ``float(`` applied to a name containing money-ish words,
   and on ``money``/``price``/``volume`` being annotated ``float``.
5. No absolute Windows drive paths (``C:\\``) in application source.
6. No naive ``datetime.now()`` / ``utcnow()`` outside ``infrastructure/clock.py``,
   ``infrastructure/logging.py`` and the CLI, all of which have a legitimate reason.

Run standalone (``python scripts/check_architecture.py``) or under pytest via
``tests/unit/test_architecture.py``.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
PACKAGE = "stop_order_scalp"

#: Layers in dependency order, innermost first. A layer may import only from layers to
#: its left.
LAYERS: tuple[str, ...] = (
    "domain",
    "infrastructure",
    "market_data",
    "integrations",
    "strategy",
    "risk",
    "trailing",
    "execution",
    "lifecycle",
    "backtest",
    "research",
    "application",
    "cli",
)

#: Layers whose business rules must stay free of MetaTrader 5 and of the broker.
BROKER_FREE_LAYERS: frozenset[str] = frozenset({"domain", "strategy", "risk", "trailing", "lifecycle"})

#: The only modules permitted to name ``MetaTrader5``.
MT5_ALLOWED: frozenset[str] = frozenset({"market_data.mt5_feed", "execution.mt5_broker"})

#: The only module permitted to name ``albrooks``.
AL_BROOKS_ALLOWED: frozenset[str] = frozenset({"integrations.al_brooks_adapter"})

#: Layer imports that are forbidden regardless of direction, because they would let
#: business rules depend on infrastructure or on the application shell.
FORBIDDEN_LAYER_IMPORTS: dict[str, frozenset[str]] = {
    "domain": frozenset(LAYERS) - {"domain"},
    "strategy": frozenset({"execution", "application", "cli", "integrations", "backtest", "research"}),
    "risk": frozenset({"execution", "application", "cli", "integrations", "backtest", "research"}),
    "trailing": frozenset({"execution", "application", "cli", "integrations", "backtest", "research"}),
    "lifecycle": frozenset({"execution", "application", "cli", "integrations", "backtest", "research"}),
    "market_data": frozenset({"strategy", "risk", "execution", "lifecycle", "application", "cli"}),
}

#: Modules allowed to read the wall clock. Everything else must take a ``Clock``.
_CLOCK_ALLOWED: frozenset[str] = frozenset(
    {"infrastructure.clock", "infrastructure.logging", "cli.main"}
)

_NAIVE_DATETIME = re.compile(r"\b(datetime|time)\s*\.\s*(now|utcnow)\s*\(")
_DRIVE_PATH = re.compile(r"[A-Za-z]:\\\\|[A-Za-z]:/Users/|/home/[a-z]+/")
_FLOAT_ANNOTATION = re.compile(r":\s*float\b")
_MONEY_NAME = re.compile(r"(money|balance|equity|profit|commission|risk|price|volume|lots|pnl)", re.I)

#: Test and script roots are not application source.
_SKIP_ROOTS = ("tests", "scripts", "build", "dist", ".venv")


@dataclass(frozen=True, slots=True)
class Violation:
    path: Path
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        try:
            relative = self.path.relative_to(REPO_ROOT)
        except ValueError:
            relative = self.path
        return f"{relative}:{self.line}: [{self.rule}] {self.message}"


def iter_sources(root: Path = SRC) -> Iterator[Path]:
    """Every application source file, excluding caches."""
    for path in sorted(root.rglob("*.py")):
        if any(part in _SKIP_ROOTS or part.startswith(".") for part in path.parts):
            continue
        yield path


def module_name(path: Path, root: Path = SRC) -> str:
    """``src/stop_order_scalp/risk/risk_manager.py`` -> ``stop_order_scalp.risk.risk_manager``."""
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def layer_of(module: str) -> str | None:
    parts = module.split(".")
    if len(parts) < 2 or parts[0] != PACKAGE:
        return None
    return parts[1]


def imported_modules(tree: ast.AST) -> Iterator[tuple[ast.Import | ast.ImportFrom, str, int]]:
    """Yield every import with its resolved absolute module name and line number."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node, alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # Relative import: walk up from the current package.
                yield node, f".{'.' * (node.level - 1)}{node.module or ''}", node.lineno
            else:
                yield node, node.module or "", node.lineno


def _function_depth(node: ast.AST, target_line: int) -> bool:
    """Whether ``target_line`` is inside any ``FunctionDef``/``AsyncFunctionDef``."""
    for candidate in ast.walk(node):
        if isinstance(candidate, ast.FunctionDef | ast.AsyncFunctionDef):
            end = getattr(candidate, "end_lineno", candidate.lineno) or candidate.lineno
            if candidate.lineno <= target_line <= end:
                return True
    return False


def check(path: Path, root: Path = SRC) -> list[Violation]:
    """All violations in one file."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - the test suite catches syntax errors first
        return [Violation(path, exc.lineno or 0, "syntax", str(exc))]

    module = module_name(path, root)
    layer = layer_of(module)
    violations: list[Violation] = []

    violations.extend(_check_text(path, source))
    violations.extend(_check_imports(path, tree, module, layer, root))
    violations.extend(_check_annotations(path, tree))

    return violations


def _check_text(path: Path, source: str) -> list[Violation]:
    violations: list[Violation] = []
    for number, line in enumerate(source.splitlines(), start=1):
        if _DRIVE_PATH.search(line) and not line.lstrip().startswith("#"):
            # A comment documenting the rule is fine; an actual path constant is not.
            violations.append(
                Violation(
                    path,
                    number,
                    "absolute-path",
                    "absolute machine path in application source; use configuration instead",
                )
            )
    return violations


def _check_imports(
    path: Path, tree: ast.AST, module: str, layer: str | None, root: Path
) -> list[Violation]:
    violations: list[Violation] = []
    for node, imported, line in imported_modules(tree):
        if not imported:
            continue

        if imported == "MetaTrader5" or imported.startswith("MetaTrader5."):
            if module not in MT5_ALLOWED:
                violations.append(
                    Violation(
                        path,
                        line,
                        "mt5-import",
                        f"MetaTrader5 may only be imported by {sorted(MT5_ALLOWED)}; found in {module}",
                    )
                )
            elif not _function_depth(tree, line):
                violations.append(
                    Violation(
                        path,
                        line,
                        "mt5-eager-import",
                        "import MetaTrader5 lazily inside a function, never at module scope",
                    )
                )

        if imported == "albrooks" or imported.startswith("albrooks."):
            if module not in AL_BROOKS_ALLOWED:
                violations.append(
                    Violation(
                        path,
                        line,
                        "albrooks-import",
                        f"albrooks may only be imported by {sorted(AL_BROOKS_ALLOWED)}; found in {module}",
                    )
                )

        if layer is None or not imported.startswith(PACKAGE):
            continue

        target_layer = layer_of(imported)
        if target_layer is None:
            continue

        if target_layer not in LAYERS:
            continue

        forbidden = FORBIDDEN_LAYER_IMPORTS.get(layer, frozenset())
        if target_layer in forbidden:
            violations.append(
                Violation(
                    path,
                    line,
                    "layer-direction",
                    f"layer '{layer}' must not import layer '{target_layer}' (from {imported})",
                )
            )

        if layer in BROKER_FREE_LAYERS and target_layer in ("execution",):
            violations.append(
                Violation(
                    path,
                    line,
                    "broker-free",
                    f"layer '{layer}' must stay independent of the broker layer",
                )
            )
    del root
    return violations


def _check_annotations(path: Path, tree: ast.AST) -> list[Violation]:
    """Reject ``float`` annotations on money-shaped names.

    A ``float`` annotation on something named ``price`` or ``commission`` is almost
    always a latent rounding bug. This is a heuristic, deliberately narrow: it never
    touches a legitimately floating quantity such as a ratio, a delay, or a score.
    """
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign | ast.arg):
            continue
        annotation = ast.unparse(node.annotation) if isinstance(node, ast.AnnAssign) else ast.unparse(node.annotation)
        if not _FLOAT_ANNOTATION.search(annotation):
            continue
        target = node.target if isinstance(node, ast.AnnAssign) else node.arg
        name = target.id if isinstance(target, ast.Name) else ast.unparse(target)
        if _MONEY_NAME.search(name) and not re.search(
            r"(fraction|ratio|ratio_|seconds|multiplier|delay|score|weight|slippage|rate|pnl_ratio)",
            name,
            re.I,
        ):
            violations.append(
                Violation(
                    path,
                    node.lineno,
                    "float-money",
                    f"{name!r} is annotated as float; use Decimal",
                )
            )
    return violations


def check_clock_usage(path: Path, tree: ast.AST, module: str) -> list[Violation]:
    """Reject implicit wall-clock reads outside the modules allowed to have them."""
    if module in _CLOCK_ALLOWED:
        return []
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = ast.unparse(node.func)
            if _NAIVE_DATETIME.search(f"{func}("):
                violations.append(
                    Violation(
                        path,
                        node.lineno,
                        "wall-clock",
                        "read time through an injected Clock, not datetime.now()/utcnow()",
                    )
                )
        elif isinstance(node, ast.ImportFrom) and node.module in ("time", "datetime"):
            for alias in node.names:
                if alias.name in ("time", "monotonic"):
                    violations.append(
                        Violation(
                            path,
                            node.lineno,
                            "wall-clock",
                            f"importing 'time' directly; use stop_order_scalp.infrastructure.clock",
                        )
                    )
    return violations


def run(root: Path = SRC) -> list[Violation]:
    violations: list[Violation] = []
    for path in iter_sources(root):
        module = module_name(path, root)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations.extend(check(path, root))
        violations.extend(check_clock_usage(path, tree, module))
    return violations


def main() -> int:
    violations = run()
    if violations:
        sys.stderr.write("architecture violations:\n")
        for violation in violations:
            sys.stderr.write(f"  {violation}\n")
        sys.stderr.write(f"\n{len(violations)} violation(s). See CONTRIBUTING.md.\n")
        return 1
    sys.stdout.write(f"architecture OK: {len(list(iter_sources()))} modules checked\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())