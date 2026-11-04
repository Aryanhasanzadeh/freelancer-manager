"""Scanning and filtering. The filtering rules are pure functions so they are testable
without touching the network."""

from __future__ import annotations

import re
import time

MY_SKILLS = {
    "PHP", "MySQL", "Laravel", "API", "MongoDB", "WordPress", "Python", "Django",
    "SQL", "Android", "Kotlin", "Flutter", "Java", "HTML", "CSS", "JavaScript",
    "iOS Development", "API Integration",
}

TERMS = [
    "python", "php", "laravel", "scraping", "automation", "api", "mysql",
    "wordpress", "bot", "script", "excel", "pdf", "telegram", "dashboard",
    "web scraping", "api integration", "data extraction",
]

# Titles that are not real coding work, or that the account should not touch.
BAD = re.compile(
    r"(design|logo|\bu[ix]\b|\bux\b|graphic|video|photo|music|voice|speech|"
    r"recording|translat|article|blog|essay|copywrit|writing|sales|cold call|"
    r"caller|telemarket|marketing|\bseo\b|data entry|typing|tutor|teach|"
    r"train|interview|research|verification|poster|greeter|receptionist|"
    r"affiliate|customer support|support specialist|crypto|trading bot|"
    r"guaranteed|revenue share|unpaid|commission)",
    re.I,
)


class Filters:
    def __init__(self, budget_min: float = 10, budget_max: float = 300, max_bids: int = 30) -> None:
        self.budget_min = budget_min
        self.budget_max = budget_max
        self.max_bids = max_bids


def to_usd(project: dict, amount) -> float:
    rate = (project.get("currency") or {}).get("exchange_rate") or 1
    return round(float(amount or 0) * float(rate), 2)


def reject_reason(project: dict, f: Filters, now: float | None = None) -> str | None:
    """Return why a project is not eligible, or None when it passes."""
    now = now or time.time()
    if project.get("language") != "en":
        return "not_english"
    if project.get("hidebids"):
        return "hidden_bids"
    jobs = {j.get("name", "") for j in project.get("jobs", [])}
    if not (jobs & MY_SKILLS):
        return "skill_mismatch"
    title = project.get("title", "")
    if BAD.search(title) or BAD.search(project.get("seo_url", "")):
        return "excluded_topic"

    budget = project.get("budget") or {}
    low = to_usd(project, budget.get("minimum"))
    if not (f.budget_min <= low <= f.budget_max):
        return "budget"

    stats = project.get("bid_stats") or {}
    if (stats.get("bid_count") or 0) > f.max_bids:
        return "too_many_bids"

    if project.get("bidperiod") and project.get("submitdate"):
        age_h = (now - project["submitdate"]) / 3600
        if project["bidperiod"] * 24 - age_h <= 0:
            return "expired"
    return None


def normalize(project: dict, now: float | None = None) -> dict:
    now = now or time.time()
    budget = project.get("budget") or {}
    cur = project.get("currency") or {}
    stats = project.get("bid_stats") or {}
    age_h = round((now - project.get("submitdate", now)) / 3600, 1)
    jobs = {j.get("name", "") for j in project.get("jobs", [])}
    return {
        "id": project["id"],
        "title": project.get("title", ""),
        "url": "https://www.freelancer.com/projects/" + project.get("seo_url", "") + "/details",
        "budget_min_usd": to_usd(project, budget.get("minimum")),
        "budget_max_usd": to_usd(project, budget.get("maximum")),
        "orig_budget": f"{budget.get('minimum')}-{budget.get('maximum')} {cur.get('code', '')}".strip(),
        "currency": cur.get("code", "USD"),
        "bids": stats.get("bid_count", 0),
        "bid_avg_usd": to_usd(project, stats.get("bid_avg")),
        "age_hours": age_h,
        "days_left": round(project.get("bidperiod", 7) - age_h / 24, 1),
        "matched_skills": sorted(jobs & MY_SKILLS),
        "description": re.sub(r"\s+", " ", project.get("description", ""))[:1500],
    }


def rank(items: list[dict]) -> list[dict]:
    """Fewest bids first, then freshest. A 20h project with 8 bids beats a 1h project with 40."""
    return sorted(items, key=lambda x: (x["bids"], x["age_hours"]))
