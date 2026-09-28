"""Multi-port entrypoint: binds one listener per ports.yaml entry, all
serving the same in-process `app` (shared registry/ledger/telegram state —
that's the whole point, one gate, many identities). Only the first port
drives the ASGI lifespan (price-refresh loop, startup banner); the rest run
with lifespan disabled so it doesn't start N duplicate background loops for
the same shared app object.
"""
from __future__ import annotations

import asyncio
import logging

import uvicorn

from app.config import settings
from app.main import app
from app.ports import all_ports

logger = logging.getLogger("llm-gate.server")


async def _run_all() -> None:
    ports = all_ports()
    if not ports:
        raise RuntimeError("ports.yaml has no entries — nothing to bind")

    servers = []
    for i, port in enumerate(ports):
        lifespan = "on" if i == 0 else "off"
        config = uvicorn.Config(app, host=settings.host, port=port, lifespan=lifespan, log_level="info")
        servers.append(uvicorn.Server(config))

    logger.info("llm-gate binding ports: %s", ports)
    await asyncio.gather(*(s.serve() for s in servers))


def main() -> None:
    asyncio.run(_run_all())


if __name__ == "__main__":
    main()
