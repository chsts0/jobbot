"""Загрузка config.yaml и .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Минимальный парсер .env, чтобы не тащить лишнюю зависимость."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    session_string: str
    anthropic_key: str | None
    data_dir: Path
    raw: dict = field(default_factory=dict)

    # удобные геттеры
    @property
    def poll_minutes(self) -> int:
        return int(self.raw.get("poll_minutes", 10))

    @property
    def backfill_hours(self) -> int:
        return int(self.raw.get("backfill_hours", 48))

    @property
    def limits(self) -> dict:
        return {"max_sends_per_day": 15, "min_delay_seconds": 90, "same_contact_days": 30,
                **(self.raw.get("limits") or {})}

    @property
    def portfolio_pdf(self) -> Path | None:
        p = self.raw.get("portfolio_pdf")
        if not p:
            return None
        path = Path(p)
        return path if path.is_absolute() else ROOT / path

    @property
    def portfolio_link(self) -> str:
        return self.raw.get("portfolio_link", "")

    @property
    def send_pdf(self) -> bool:
        return bool(self.raw.get("send_pdf", True))

    @property
    def llm_enabled(self) -> bool:
        return bool((self.raw.get("llm") or {}).get("enabled", True)) and bool(self.anthropic_key)

    @property
    def llm_model(self) -> str:
        return (self.raw.get("llm") or {}).get("model", "claude-sonnet-4-5")

    @property
    def min_score(self) -> int:
        return int(self.raw.get("min_score", 3))

    @property
    def profile(self) -> str:
        # В публичном репозитории факты о тебе лучше держать в секрете JOBBOT_PROFILE, а не в config.yaml
        return os.environ.get("JOBBOT_PROFILE") or self.raw.get("profile", "")

    @property
    def only_direct(self) -> bool:
        return bool(self.raw.get("only_direct", True))

    @property
    def templates(self) -> dict:
        return self.raw.get("templates") or {}

    @property
    def keywords(self) -> dict:
        return self.raw.get("keywords") or {}

    @property
    def channels(self) -> dict[str, list[str]]:
        return self.raw.get("channels") or {}


def load_config(config_path: Path | None = None, require_secrets: bool = True) -> Config:
    _load_dotenv(ROOT / ".env")
    config_path = config_path or ROOT / "config.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    def need(name: str) -> str:
        value = os.environ.get(name, "")
        if require_secrets and not value:
            raise SystemExit(f"Нет переменной {name} в .env — см. README, шаг 2")
        return value

    data_dir = Path(os.environ.get("DATA_DIR", ROOT / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Config(
        # TG_API_ID / TG_API_HASH / TG_SESSION нужны только для полного режима
        api_id=int(os.environ.get("TG_API_ID") or 0),
        api_hash=os.environ.get("TG_API_HASH", ""),
        bot_token=need("BOT_TOKEN"),
        session_string=os.environ.get("TG_SESSION", ""),
        anthropic_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        data_dir=data_dir,
        raw=raw,
    )
