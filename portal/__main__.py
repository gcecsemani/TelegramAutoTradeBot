"""python3 -m portal"""

from __future__ import annotations

import uvicorn

import config
from portal.app import app


def main() -> None:
    uvicorn.run(app, host=config.PORTAL_HOST, port=config.PORTAL_PORT, log_level="info")


if __name__ == "__main__":
    main()
