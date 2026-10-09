"""Dedicated public server entrypoint; never registers or imports Prism's general CLI."""

from __future__ import annotations

import os

from market_signal.context.gateway.auth import Verifier
from market_signal.context.gateway.server import create_app
from market_signal.context.gateway.spool import Spool, default_root


def run(*, dev=False):
    import uvicorn

    if dev and os.environ.get("RAILWAY_ENVIRONMENT_ID"):
        raise ValueError("development HTTP is forbidden on Railway")
    verifier = Verifier.environment()
    app = create_app(Spool(default_root()), verifier, dev=dev)
    uvicorn.run(
        app,
        host="127.0.0.1" if dev else "0.0.0.0",
        port=int(os.environ.get("PORT", "8787")),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("PRISM_CONTEXT_TRUSTED_PROXIES", "127.0.0.1"),
        access_log=False,
        limit_concurrency=32,
        timeout_keep_alive=5,
        log_level="critical",
    )


if __name__ == "__main__":
    run()
