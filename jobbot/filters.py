"""Фильтрация постов: подходит ли вакансия, её балл, контакты, язык."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

USERNAME_RE = re.compile(r"(?<![\w@/.])@([A-Za-z][A-Za-z0-9_]{3,31})\b")
TME_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{3,31})(?![\w/])", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
URL_RE = re.compile(r"https?://[^\s)>\]]+", re.I)

# Служебные ники, которым писать бессмысленно
IGNORED_USERNAMES = {"joinchat", "addlist", "share", "proxy", "gmail", "yandex", "mail"}


@dataclass
class Verdict:
    ok: bool
    score: int = 0
    reason: str = ""
    hits: list[str] = field(default_factory=list)


@dataclass
class Contacts:
    telegram: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)

    @property
    def primary_tg(self) -> str | None:
        """Первый живой человек. Боты (…bot) не подходят — отклик через бота делается руками."""
        for name in self.telegram:
            if not name.lower().endswith("bot"):
                return name
        return None


class VacancyFilter:
    def __init__(self, keywords: dict):
        flags = re.I | re.U
        self.must = [re.compile(p, flags) for p in keywords.get("must", [])]
        self.stop = [re.compile(p, flags) for p in keywords.get("stop", [])]
        self.plus = [(re.compile(p, flags), int(w)) for p, w in (keywords.get("plus") or {}).items()]
        self.minus = [(re.compile(p, flags), int(w)) for p, w in (keywords.get("minus") or {}).items()]

    def check(self, text: str) -> Verdict:
        if not text or len(text) < 60:
            return Verdict(False, reason="слишком короткий")
        for rx in self.stop:
            m = rx.search(text)
            if m:
                return Verdict(False, reason=f"стоп-слово: {m.group(0)}")
        if not any(rx.search(text) for rx in self.must):
            return Verdict(False, reason="нет ключевых слов")
        score, hits = 0, []
        for rx, w in self.plus:
            m = rx.search(text)
            if m:
                score += w
                hits.append(f"+{w} {m.group(0).lower()}")
        for rx, w in self.minus:
            m = rx.search(text)
            if m:
                score -= w
                hits.append(f"−{w} {m.group(0).lower()}")
        return Verdict(True, score=score, hits=hits)


def extract_contacts(text: str, own_username: str | None = None,
                     entity_urls: list[str] | None = None) -> Contacts:
    """Достаёт @ники, ссылки t.me, почты и прочие ссылки из поста.

    entity_urls — ссылки, спрятанные под текстом (MessageEntityTextUrl), их тоже проверяем.
    """
    own = (own_username or "").lower()
    blob = text + "\n" + "\n".join(entity_urls or [])
    tg: list[str] = []

    def add(name: str) -> None:
        low = name.lower()
        if low == own or low in IGNORED_USERNAMES or low in (x.lower() for x in tg):
            return
        tg.append(name)

    emails = []
    for m in EMAIL_RE.finditer(blob):
        e = m.group(0).rstrip(".")
        if e not in emails:
            emails.append(e)
    email_spans = [m.span() for m in EMAIL_RE.finditer(blob)]

    for m in TME_RE.finditer(blob):
        add(m.group(1))
    for m in USERNAME_RE.finditer(blob):
        if any(s <= m.start() < e for s, e in email_spans):
            continue
        add(m.group(1))

    urls = []
    for m in URL_RE.finditer(blob):
        u = m.group(0).rstrip(".,;")
        if TME_RE.match(u):
            continue
        if u not in urls:
            urls.append(u)
    return Contacts(telegram=tg, emails=emails, urls=urls)


def detect_lang(text: str) -> str:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "ru"
    cyr = sum(1 for c in letters if "а" <= c.lower() <= "я" or c.lower() == "ё")
    return "ru" if cyr / len(letters) > 0.3 else "en"


def fingerprint(text: str) -> str:
    """Отпечаток вакансии — чтобы одна и та же вакансия из разных каналов пришла один раз."""
    norm = re.sub(r"https?://\S+|\S*@\S+|t\.me/\S+", " ", text.lower())
    norm = re.sub(r"[^\w]+", " ", norm)
    norm = re.sub(r"\s+", " ", norm).strip()[:400]
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def classify(text: str) -> str:
    """Грубый тип вакансии — для выбора шаблона письма."""
    t = text.lower()
    if re.search(r"лендинг|landing|брендинг|brand|айдентик|логотип|сайт под ключ|tilda|тильд", t) and not re.search(
            r"product designer|продуктов\w* дизайнер", t):
        return "brand_landing"
    return "product"
