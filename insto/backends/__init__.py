"""Backend factory.

`make_backend(name, **opts)` is the single entry point used by the service
facade to construct a backend. Concrete backend modules are imported lazily
so that pulling in `insto.backends` does not pay the cost (and surface the
runtime dependency footprint) of every backend at once.

Practically: `import insto` does not import `hikerapi`. Only the
`make_backend("hiker", ...)` call does — and that import lives inside the
function body.
"""

from __future__ import annotations

from typing import Any

from insto.backends._base import OSINTBackend

__all__ = ["OSINTBackend", "make_backend"]


def make_backend(name: str, **opts: Any) -> OSINTBackend:
    """Construct a backend by short name.

    Known names:
        "hiker" — `HikerBackend` (HikerAPI SDK). Imports `hikerapi` lazily.

    Raises `ValueError` for unknown backend names.
    """
    if name == "hiker":
        from insto.backends.hiker import HikerBackend

        return HikerBackend(**opts)
    raise ValueError(f"unknown backend: {name!r}")
