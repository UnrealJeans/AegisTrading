"""Container health probe for non-HTTP services: `python -m aegis.services.healthcheck <service>`."""
from __future__ import annotations

import asyncio
import sys
import time

from aegis.core.bus import Bus


async def check(service: str) -> int:
    bus = Bus.from_url()
    try:
        hb = (await bus.heartbeats()).get(service)
        return 0 if hb and time.time() - hb["ts"] < 90 else 1
    except Exception:
        return 1
    finally:
        await bus.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(check(sys.argv[1])))
