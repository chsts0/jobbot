"""Один раз: вход в твой Telegram и получение строки сессии для .env (TG_SESSION).

Запусти у себя на компьютере:  python login.py
Telegram пришлёт код в приложение — введи его здесь. Пароль (если включена 2FA) тоже вводишь ты сам.
Строку сессии никому не показывай: это полный доступ к аккаунту.
"""
import os

from telethon.sessions import StringSession
from telethon.sync import TelegramClient

from jobbot.config import _load_dotenv, ROOT

_load_dotenv(ROOT / ".env")
api_id = int(os.environ.get("TG_API_ID") or input("TG_API_ID: "))
api_hash = os.environ.get("TG_API_HASH") or input("TG_API_HASH: ")

with TelegramClient(StringSession(), api_id, api_hash) as client:
    session = client.session.save()
    me = client.get_me()
    print(f"\nВошёл как @{me.username}. Скопируй строку ниже в .env после TG_SESSION=\n")
    print(session)
    env = ROOT / ".env"
    if env.exists() and "TG_SESSION=" in env.read_text(encoding="utf-8") and \
            input("\nЗаписать её в .env автоматически? [y/N] ").strip().lower() == "y":
        lines = [f"TG_SESSION={session}" if l.startswith("TG_SESSION=") else l
                 for l in env.read_text(encoding="utf-8").splitlines()]
        env.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("Готово, записал.")
