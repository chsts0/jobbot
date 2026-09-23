"""Тексты откликов: короткий шаблон (по умолчанию) или нейросеть."""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты пишешь короткий отклик на вакансию от лица дизайнера — личное сообщение в Telegram рекрутеру.
Пиши от первого лица, просто и по-человечески. Язык письма: {lang_name}. 2–3 предложения: приветствие,
по какой вакансии пишешь, ссылка на портфолио и резюме ({portfolio_link}). Ничего не выдумывай.

Профиль:
{profile}

Выведи только текст сообщения."""

SHORT = {
    "ru": ("Здравствуйте! Пишу по поводу вакансии{role}. Я продуктовый дизайнер, "
           "портфолио и резюме здесь: {portfolio_link}\nБуду рад пообщаться."),
    "en": ("Hi! I'm reaching out about the{role} position. I'm a product designer, "
           "my portfolio and CV are here: {portfolio_link}\nHappy to chat."),
}


def template_letter(text: str, lang: str, portfolio_link: str, role: str = "", templates: dict | None = None) -> str:
    """Короткое нейтральное сообщение: привет, по какой вакансии, ссылка. Дальше диалог ведёшь сам."""
    lang = lang if lang in ("ru", "en") else "ru"
    tpl = (templates or {}).get(lang) or SHORT[lang]
    if role:
        if lang == "ru":
            # «продуктового дизайнера» уже в нужном падеже — без кавычек; «UI/UX дизайнер» — в кавычках
            role_part = f" {role}" if role.lower().endswith("дизайнера") else f" «{role}»"
        else:
            role_part = f" {role}"
    else:
        role_part = " дизайнера" if lang == "ru" else " designer"
    return tpl.strip().format(portfolio_link=portfolio_link, role=role_part)


ROLE_RE = re.compile(
    r"(?:(?:senior|middle\+?|junior|lead|старш\w*|ведущ\w*|младш\w*)\s+)?"
    r"(?:(?:product|ui\s*/\s*ux|ux\s*/\s*ui|ux|ui|web|веб|продуктов\w*|графическ\w*|graphic|visual|brand)"
    r"(?:\s*/\s*(?:ui|ux|web|веб))?[\s-]*)+(?:designer|дизайнер\w*)"
    r"(?:\s*\((?:ui\s*/\s*ux|ux\s*/\s*ui|ui|ux|web)\))?",
    re.I)


def guess_role(text: str) -> str:
    """Должность из текста вакансии: «Product Designer (UI/UX)», «UI/UX дизайнер»… Не нашлась — пусто."""
    m = ROLE_RE.search(text)
    if not m:
        return ""
    role = re.sub(r"\s+", " ", m.group(0)).strip(" -")
    return role if len(role) <= 50 else ""


class LetterWriter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = None
        if cfg.llm_enabled:
            from anthropic import AsyncAnthropic
            self.client = AsyncAnthropic(api_key=cfg.anthropic_key)

    @property
    def mode(self) -> str:
        return f"нейросеть ({self.cfg.llm_model})" if self.client else "шаблоны"

    async def write(self, vacancy_text: str, lang: str, extra_instruction: str = "") -> str:
        role = guess_role(vacancy_text)
        if not self.client:
            return template_letter(vacancy_text, lang, self.cfg.portfolio_link, role, self.cfg.templates)
        system = SYSTEM_PROMPT.format(
            lang_name="русский" if lang == "ru" else "английский",
            portfolio_link=self.cfg.portfolio_link,
            profile=self.cfg.profile,
        )
        user = f"Вакансия:\n\n{vacancy_text[:6000]}"
        if extra_instruction:
            user += f"\n\nДополнительное пожелание к письму: {extra_instruction}"
        try:
            resp = await self.client.messages.create(
                model=self.cfg.llm_model, max_tokens=600, system=system,
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
            if text:
                return text
        except Exception as e:  # сеть, лимиты, неверный ключ — не роняем бота
            log.warning("LLM недоступна, беру шаблон: %s", e)
        return template_letter(vacancy_text, lang, self.cfg.portfolio_link, role, self.cfg.templates)
