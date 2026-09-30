"""Single source of truth for the distribution version.

Kept in its own module so that packaging metadata and runtime code cannot drift.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
