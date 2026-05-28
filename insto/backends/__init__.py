"""Backend factory.

`make_backend(name, **opts)` is the single entry point used by the service
facade to construct a backend. Concrete backend modules are imported lazily
so that pulling in `insto.backends` does not pay the cost (and surface the
runtime dependency footprint) of every backend at once.

Practically: `import insto` does not import `hikerapi`. Only the
`make_backend("hikerapi", ...)` call does — and that import lives inside the
function body.

Setting `INSTO_BACKEND=fake` in the environment overrides the requested
name with `"fake"` — a self-contained, network-free backend used by E2E
tests. The override is intentionally global so the same CLI / REPL entry
points the user runs are exercised end-to-end without test-only patches.
"""

from __future__ import annotations

import os
from typing import Any

from insto.backends._base import OSINTBackend
from insto.config import BACKEND_AIOGRAPI, BACKEND_FAKE, BACKEND_HIKERAPI, LEGACY_BACKEND_HIKER

BACKEND_OVERRIDE_ENV = "INSTO_BACKEND"
AIOGRAPI_INSTALL_HINT = (
    "aiograpi backend requested but the `aiograpi` package is not installed. "
    "For an existing pipx install, run: `pipx inject insto aiograpi`. "
    "Fresh installs can use: `pipx install 'insto[aiograpi]'` or "
    "`uv tool install --force 'insto[aiograpi]'`."
)

__all__ = ["BACKEND_OVERRIDE_ENV", "OSINTBackend", "make_backend"]


def make_backend(name: str, **opts: Any) -> OSINTBackend:
    """Construct a backend by short name.

    Known names:
        "hikerapi" — `HikerBackend` (HikerAPI SDK). Imports `hikerapi` lazily.
        "hiker" — legacy alias for "hikerapi".
        "fake"  — `FakeBackendProd`, hardcoded in-process data for E2E
                  tests. Selected when `INSTO_BACKEND=fake` is set even if
                  the caller asked for another backend.

    Raises `ValueError` for unknown backend names.
    """
    override = os.environ.get(BACKEND_OVERRIDE_ENV)
    if override:
        name = override
    if name in {BACKEND_HIKERAPI, LEGACY_BACKEND_HIKER}:
        from insto.backends.hiker import HikerBackend

        return HikerBackend(**opts)
    if name == BACKEND_AIOGRAPI:
        # aiograpi is an optional dependency: gate the import so the
        # default install (hiker-only) does not have to ship it. If the
        # user did not install `insto[aiograpi]`, give them the exact
        # command to run.
        try:
            from insto.backends.aiograpi import AiograpiBackend

            return AiograpiBackend(**opts)
        except ModuleNotFoundError as exc:  # pragma: no cover — environment dependent
            missing = getattr(exc, "name", None)
            if missing == "aiograpi" or "aiograpi" in str(exc):
                raise RuntimeError(AIOGRAPI_INSTALL_HINT) from exc
            raise
    if name == BACKEND_FAKE:
        from insto.backends._fake import FakeBackendProd

        return FakeBackendProd(**opts)
    raise ValueError(f"unknown backend: {name!r}")
