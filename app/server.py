"""Multi-port entrypoint: binds one listener per ports.yaml entry, all
serving the same in-process `app` (shared registry/ledger/telegram state —
that's the whole point, one gate, many identities). Only the first port
drives the ASGI lifespan (price-refresh loop, startup banner); the rest run
with lifespan disabled so it doesn't start N duplicate background loops for
the same shared app object.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import subprocess

import uvicorn

from app.config import settings
from app.main import app
from app.ports import all_ports, identity_for_port

logger = logging.getLogger("llm-gate.server")


async def _tailscale_ip(wait_seconds: int = 90) -> str | None:
    """The i7's Tailscale IPv4, retried while tailscaled comes up after boot.
    Returns None rather than ever falling back to a wildcard bind."""
    for _ in range(wait_seconds // 5):
        try:
            out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5)
            ip = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
            if ip and ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10"):
                return ip
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        await asyncio.sleep(5)
    return None


async def _run_all() -> None:
    ports = all_ports()
    if not ports:
        raise RuntimeError("ports.yaml has no entries — nothing to bind")

    servers = []
    for i, port in enumerate(ports):
        lifespan = "on" if i == 0 else "off"
        config = uvicorn.Config(app, host=settings.host, port=port, lifespan=lifespan, log_level="info")
        servers.append(uvicorn.Server(config))

    # I7_MAIN §4: ports flagged bind_tailscale also listen on the Tailscale IP
    # (CGNAT 100.64/10 only — validated above), everything else stays loopback.
    # Started alongside, not before, the loopback servers so a slow tailscaled
    # at boot never delays the gate itself.
    async def _serve_tailscale() -> None:
        ts_ports = [p for p in ports if identity_for_port(p).bind_tailscale]
        if not ts_ports:
            return
        ts_ip = await _tailscale_ip()
        if ts_ip is None:
            logger.error("tailscale ip unavailable; %s stay loopback-only", ts_ports)
            return
        logger.info("tailscale %s binding ports: %s", ts_ip, ts_ports)
        await asyncio.gather(*(
            uvicorn.Server(uvicorn.Config(app, host=ts_ip, port=p, lifespan="off",
                                          log_level="info")).serve()
            for p in ts_ports
        ))

    logger.info("llm-gate binding ports: %s", ports)
    await asyncio.gather(*(s.serve() for s in servers), _serve_tailscale())


def main() -> None:
    asyncio.run(_run_all())


if __name__ == "__main__":
    main()
