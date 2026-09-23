"""Запуск:
    python main.py          — постоянный режим (компьютер или сервер)
    python main.py --once   — один проход и выход
    python main.py --for 50 — работать 50 минут и выйти (так запускает GitHub Actions)
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
    if "--for" in sys.argv:  # python main.py --for 50  — работать 50 минут и выйти (GitHub Actions)
        minutes = float(sys.argv[sys.argv.index("--for") + 1])
        asyncio.run(bot.run_for(minutes))
    elif "--once" in sys.argv:
        asyncio.run(bot.run_once())
    else:
        asyncio.run(bot.run())


if __name__ == "__main__":
    main()
