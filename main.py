"""Запуск:
    python main.py          — постоянный режим (компьютер или сервер)
    python main.py --once   — один проход и выход (GitHub Actions по расписанию)
"""
import asyncio
import logging
import sys

from jobbot.app import JobBot
from jobbot.config import load_config


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config()
    bot = JobBot(cfg)
    asyncio.run(bot.run_once() if "--once" in sys.argv else bot.run())


if __name__ == "__main__":
    main()
