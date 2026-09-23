"""SQLite: состояние каналов, найденные вакансии, история отправок."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    username TEXT PRIMARY KEY,
    region   TEXT NOT NULL,
    last_id  INTEGER NOT NULL DEFAULT 0,
    enabled  INTEGER NOT NULL DEFAULT 1,
    error    TEXT
);
CREATE TABLE IF NOT EXISTS vacancies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT UNIQUE,
    channel     TEXT,
    msg_id      INTEGER,
    link        TEXT,
    text        TEXT,
    region      TEXT,
    lang        TEXT,
    score       INTEGER,
    hits        TEXT,
    contact_tg  TEXT,
    contacts    TEXT,
    letter      TEXT,
    status      TEXT NOT NULL DEFAULT 'new',   -- new | sent | skipped | failed
    card_msg_id INTEGER,
    letter_msg_id INTEGER,
    error       TEXT,
    created_at  REAL,
    sent_at     REAL
);
CREATE TABLE IF NOT EXISTS sends (
    contact TEXT,
    sent_at REAL
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


@dataclass
class Vacancy:
    id: int
    channel: str
    msg_id: int
    link: str
    text: str
    region: str
    lang: str
    score: int
    hits: list[str]
    contact_tg: str | None
    contacts: dict
    letter: str
    status: str
    card_msg_id: int | None
    letter_msg_id: int | None


class DB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ── каналы ────────────────────────────────────────────
    def sync_channels(self, channels: dict[str, list[str]]) -> None:
        for region, names in channels.items():
            for name in names:
                name = name.lstrip("@").strip()
                self.conn.execute(
                    "INSERT INTO channels(username, region) VALUES(?, ?) "
                    "ON CONFLICT(username) DO UPDATE SET region=excluded.region", (name, region))
        self.conn.commit()

    def add_channel(self, username: str, region: str) -> None:
        self.conn.execute(
            "INSERT INTO channels(username, region, enabled) VALUES(?, ?, 1) "
            "ON CONFLICT(username) DO UPDATE SET region=excluded.region, enabled=1, error=NULL",
            (username.lstrip("@"), region))
        self.conn.commit()

    def disable_channel(self, username: str) -> bool:
        cur = self.conn.execute("UPDATE channels SET enabled=0 WHERE lower(username)=lower(?)",
                                (username.lstrip("@"),))
        self.conn.commit()
        return cur.rowcount > 0

    def active_channels(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM channels WHERE enabled=1 ORDER BY region, username"))

    def all_channels(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM channels ORDER BY region, username"))

    def set_last_id(self, username: str, last_id: int) -> None:
        self.conn.execute("UPDATE channels SET last_id=?, error=NULL WHERE username=?", (last_id, username))
        self.conn.commit()

    def set_channel_error(self, username: str, error: str, disable: bool = False) -> None:
        self.conn.execute("UPDATE channels SET error=?, enabled=CASE WHEN ? THEN 0 ELSE enabled END "
                          "WHERE username=?", (error[:300], int(disable), username))
        self.conn.commit()

    # ── вакансии ──────────────────────────────────────────
    def seen(self, fp: str) -> bool:
        return self.conn.execute("SELECT 1 FROM vacancies WHERE fingerprint=?", (fp,)).fetchone() is not None

    def add_vacancy(self, **kw) -> int | None:
        kw.setdefault("created_at", time.time())
        kw["hits"] = json.dumps(kw.get("hits") or [], ensure_ascii=False)
        kw["contacts"] = json.dumps(kw.get("contacts") or {}, ensure_ascii=False)
        cols = ",".join(kw)
        try:
            cur = self.conn.execute(f"INSERT INTO vacancies({cols}) VALUES({','.join('?' * len(kw))})",
                                    tuple(kw.values()))
        except sqlite3.IntegrityError:
            return None
        self.conn.commit()
        return cur.lastrowid

    def get(self, vid: int) -> Vacancy | None:
        r = self.conn.execute("SELECT * FROM vacancies WHERE id=?", (vid,)).fetchone()
        if not r:
            return None
        return Vacancy(id=r["id"], channel=r["channel"], msg_id=r["msg_id"], link=r["link"], text=r["text"],
                       region=r["region"], lang=r["lang"], score=r["score"], hits=json.loads(r["hits"] or "[]"),
                       contact_tg=r["contact_tg"], contacts=json.loads(r["contacts"] or "{}"),
                       letter=r["letter"] or "", status=r["status"], card_msg_id=r["card_msg_id"],
                       letter_msg_id=r["letter_msg_id"])

    def update(self, vid: int, **kw) -> None:
        sets = ",".join(f"{k}=?" for k in kw)
        self.conn.execute(f"UPDATE vacancies SET {sets} WHERE id=?", (*kw.values(), vid))
        self.conn.commit()

    def undelivered(self, limit: int = 15) -> list[int]:
        rows = self.conn.execute("SELECT id FROM vacancies WHERE status='new' AND letter_msg_id IS NULL "
                                 "ORDER BY score DESC, id DESC LIMIT ?", (limit,)).fetchall()
        return [r["id"] for r in rows]

    # ── лимиты ────────────────────────────────────────────
    def record_send(self, contact: str) -> None:
        self.conn.execute("INSERT INTO sends(contact, sent_at) VALUES(?, ?)", (contact.lower(), time.time()))
        self.conn.commit()

    def sends_last_24h(self) -> int:
        return self.conn.execute("SELECT count(*) FROM sends WHERE sent_at > ?",
                                 (time.time() - 86400,)).fetchone()[0]

    def last_send_at(self) -> float:
        r = self.conn.execute("SELECT max(sent_at) FROM sends").fetchone()[0]
        return r or 0.0

    def contacted_recently(self, contact: str, days: int) -> bool:
        return self.conn.execute("SELECT 1 FROM sends WHERE contact=? AND sent_at > ?",
                                 (contact.lower(), time.time() - days * 86400)).fetchone() is not None

    # ── статистика и флаги ────────────────────────────────
    def stats(self) -> dict:
        rows = self.conn.execute("SELECT status, count(*) c FROM vacancies GROUP BY status").fetchall()
        out = {r["status"]: r["c"] for r in rows}
        out["sent_24h"] = self.sends_last_24h()
        return out

    def get_flag(self, k: str, default: str = "") -> str:
        r = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    def set_flag(self, k: str, v: str) -> None:
        self.conn.execute("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        self.conn.commit()
