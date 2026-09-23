"""Чтение публичных каналов через веб-версию t.me/s/<канал> — без входа в аккаунт и без api_id."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import aiohttp
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0 Safari/537.36",
    "Accept-Language": "ru,en;q=0.8",
}
MAX_PAGES = 5  # по 20 постов — не больше 100 за проход на канал


class ChannelUnavailable(Exception):
    """Канал закрыт, это чат, а не канал, или у него выключен веб-просмотр."""


@dataclass
class Post:
    id: int
    text: str
    date: datetime
    links: list[str] = field(default_factory=list)


def parse_page(html: str) -> tuple[list[Post], bool]:
    """Возвращает посты страницы (по возрастанию id) и признак, что это вообще лента канала."""
    soup = BeautifulSoup(html, "html.parser")
    if not soup.select_one(".tgme_channel_history"):
        return [], False
    posts = []
    for node in soup.select(".tgme_widget_message[data-post]"):
        try:
            pid = int(node["data-post"].rsplit("/", 1)[1])
        except (ValueError, IndexError):
            continue
        text_node = node.select_one(".tgme_widget_message_text")
        if text_node is None:
            continue  # только картинка/видео без текста
        for br in text_node.find_all("br"):
            br.replace_with("\n")
        text = text_node.get_text().strip()
        links = [a["href"] for a in text_node.find_all("a", href=True)]
        t = node.select_one("time[datetime]")
        try:
            date = datetime.fromisoformat(t["datetime"]) if t else datetime.now(timezone.utc)
        except ValueError:
            date = datetime.now(timezone.utc)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        posts.append(Post(id=pid, text=text, date=date, links=links))
    posts.sort(key=lambda p: p.id)
    return posts, True


class WebReader:
    def __init__(self):
        self.session: aiohttp.ClientSession | None = None

    async def _get(self, url: str) -> str:
        if self.session is None:
            self.session = aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30))
        async with self.session.get(url, allow_redirects=True) as r:
            if r.status >= 400:
                raise ChannelUnavailable(f"HTTP {r.status}")
            return await r.text()

    async def fetch_new(self, username: str, last_id: int, backfill_hours: int) -> tuple[list[Post], int]:
        """Новые посты после last_id (или за последние backfill_hours при первом запуске).

        Возвращает (посты, максимальный_виденный_id)."""
        since = datetime.now(timezone.utc) - timedelta(hours=backfill_hours)
        url = f"https://t.me/s/{username}"
        collected: dict[int, Post] = {}
        max_seen = last_id
        for _ in range(MAX_PAGES):
            posts, ok = parse_page(await self._get(url))
            if not ok:
                raise ChannelUnavailable("нет веб-просмотра (скорее всего это чат, а не канал)")
            if not posts:
                break
            max_seen = max(max_seen, posts[-1].id)
            for p in posts:
                if (last_id and p.id > last_id) or (not last_id and p.date >= since):
                    collected[p.id] = p
            oldest = posts[0]
            done = oldest.id <= last_id + 1 if last_id else oldest.date < since
            if done:
                break
            url = f"https://t.me/s/{username}?before={oldest.id}"
        return sorted(collected.values(), key=lambda p: p.id), max_seen

    async def close(self) -> None:
        if self.session:
            await self.session.close()
