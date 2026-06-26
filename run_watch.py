from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kinvest_trade.cli import run_watch_console
from kinvest_trade.config import load_app_config


if __name__ == "__main__":
    config = load_app_config()
    if os.getenv("KIS_WATCH_MAX_CYCLES"):
        config.watch.max_cycles = int(os.getenv("KIS_WATCH_MAX_CYCLES", "0"))
    if os.getenv("KIS_WATCH_POLL_INTERVAL_SEC"):
        config.watch.poll_interval_sec = int(os.getenv("KIS_WATCH_POLL_INTERVAL_SEC", "15"))
    asyncio.run(run_watch_console(config))
