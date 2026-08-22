#!/usr/bin/env python3
from __future__ import annotations

import logging
import sys

from beauty_bot.bot import BeautyBot
from beauty_bot.config import Settings


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings.from_env()
    except (ValueError, TypeError) as exc:
        print(f"Ошибка настроек: {exc}", file=sys.stderr)
        return 1
    BeautyBot(settings).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
