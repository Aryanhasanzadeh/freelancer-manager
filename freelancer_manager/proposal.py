"""Proposal drafting through whatever endpoint /ai was last configured with."""

from __future__ import annotations

from .aisettings import AISettings, chat

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


def draft_proposal(title: str, description: str, budget: dict, skills: list[str],
                   amount: float, period: int, settings: AISettings | None = None) -> str:
    if settings is None:
        settings = AISettings.load()

    prompt = (
        f"Project title: {title}\n"
        f"Budget: {budget.get('minimum')}-{budget.get('maximum')} {budget.get('currency', 'USD')}\n"
        f"Required skills: {', '.join(skills) or 'none listed'}\n"
        f"My bid: ${amount:.0f} fixed, delivered in {period} days\n"
        f"Full brief:\n{description[:4000]}\n\n"
        "Write the bid text now."
    )

    try:
        return chat(
            settings.base_url, settings.api_key, settings.model,
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": prompt}],
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
            timeout=settings.timeout,
        )
    except RuntimeError as exc:
        raise DraftError(str(exc)) from exc
