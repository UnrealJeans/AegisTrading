from __future__ import annotations

import uvicorn

from aegis.api.app import create_app
from aegis.config import get_settings
from aegis.core.logging import configure_logging
from aegis.db.session import init_engine


def app_factory():  # type: ignore[no-untyped-def]
    settings = get_settings()
    configure_logging("backend", settings.log_level, settings.log_json)
    init_engine(settings.database_url)
    return create_app(settings)


if __name__ == "__main__":
    uvicorn.run("aegis.services.api:app_factory", factory=True, host="0.0.0.0", port=8000, proxy_headers=False, access_log=False,
                timeout_graceful_shutdown=15)
