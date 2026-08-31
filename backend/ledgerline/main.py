"""FastAPI entrypoint.

Deliberately thin for now.  The health endpoint exists so docker-compose and CI
have something to assert against before there is any real surface.
"""

from __future__ import annotations

from fastapi import FastAPI

from . import __version__

app = FastAPI(title="Ledgerline", version=__version__)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
