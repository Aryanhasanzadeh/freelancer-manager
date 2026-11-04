"""Proposal drafting through an OpenAI-compatible chat endpoint."""

from __future__ import annotations

import os
from pathlib import Path

import requests

from .config import SECRETS

SYSTEM = """You write Freelancer.com bids for Aryan Hasanzadeh (username aryanhz), \
a senior developer with 10+ years of experience in PHP/Laravel, Python, MySQL, \
JavaScript and API integrations. He builds scrapers, automation pipelines, \
dashboards and Telegram bots.

Rules for every bid:
- Plain English, no markdown, no emoji, no headings.
- Start with one sentence that proves you read the brief (name the actual deliverable).
- Then 3 to 4 short numbered lines describing how you would build it.
- Then one line on deliverables and one line on why it will not break in production.
- Never invent experience, client names, or certifications.
- Never promise a guaranteed result, accuracy figure or revenue.
- If the brief is vague, ask 1 to 2 concrete questions instead of padding.
- 120 to 200 words. No sign-off, no "let me know" filler at the end.
"""


class DraftError(RuntimeError):
    pass


def _key() -> str:
    p = SECRETS / "ai_key.txt"
    return os.environ.get("FL_AI_KEY") or (p.read_text().strip() if p.exists() else "")


def draft_proposal(title: str, description: str, budget: dict, skills: list[str],
                   amount: float, period: int) -> str:
    key = _key()
    if not key:
        raise DraftError("no AI key: put one in secrets/ai_key.txt or set FL_AI_KEY")

    base = os.environ.get("FL_AI_BASE", "https://ai.metft.ir/v1").rstrip("/")
    model = os.environ.get("FL_AI_MODEL", "glm-5.3-flash")

    prompt = (
        f"Project title: {title}\n"
        f"Budget: {budget.get('minimum')}-{budget.get('maximum')} {budget.get('currency', 'USD')}\n"
        f"Required skills: {', '.join(skills) or 'none listed'}\n"
        f"My bid: ${amount:.0f} fixed, delivered in {period} days\n"
        f"Full brief:\n{description[:4000]}\n\n"
        "Write the bid text now."
    )

    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.4,
            "max_tokens": 700,
        },
        timeout=120,
    )
    if r.status_code >= 400:
        raise DraftError(f"LLM {r.status_code}: {r.text[:200]}")
    data = r.json()
    text = (data["choices"][0]["message"]["content"] or "").strip()
    if not text:
        raise DraftError("LLM returned an empty draft")
    return text
