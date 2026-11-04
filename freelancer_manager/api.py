"""Minimal Freelancer.com API client: session, project scan, bid write, bid list."""

from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any

import requests

from .config import Config

UA = "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:157.0) Gecko/20100101 Firefox/157.0"

COMMON = "webapp=1&compact=true&new_errors=true&new_pools=true"


class FreelancerError(RuntimeError):
    def __init__(self, message: str, code: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class Client:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": UA,
                "Accept": "application/json",
                "Referer": "https://www.freelancer.com/",
            }
        )
        cookies = cfg.cookies()
        for k, v in cookies.items():
            self.s.cookies.set(k, v, domain=".freelancer.com")
        self.uid = cookies.get("GETAFREE_USER_ID", cfg.user_id)
        raw = urllib.parse.unquote(cookies.get("GETAFREE_AUTH_HASH_V2", ""))
        if raw:
            # POST endpoints reject an unauthenticated session even with valid cookies.
            self.s.headers["freelancer-auth-v2"] = f"{self.uid};{raw}"
        xsrf = urllib.parse.unquote(cookies.get("XSRF-TOKEN", ""))
        if xsrf:
            self.s.headers["X-XSRF-TOKEN"] = xsrf

    # ---------------------------------------------------------------- read
    def _get(self, path: str, **params: Any) -> dict:
        url = f"{self.cfg.base}{path}?{COMMON}&{urllib.parse.urlencode(params, doseq=True)}"
        r = self.s.get(url, timeout=40)
        try:
            data = r.json()
        except ValueError:
            raise FreelancerError(f"non-JSON response ({r.status_code})", status=r.status_code)
        if data.get("status") != "success":
            raise FreelancerError(
                data.get("message", "api error"),
                code=(data.get("error") or {}).get("code", ""),
                status=r.status_code,
            )
        return data

    def self_info(self) -> dict:
        """Identity check. A logged-out session also answers 200, so compare the username."""
        data = self._get("/users/0.1/self/", jobs="true")
        return data["result"]

    def verify_session(self) -> str:
        info = self.self_info()
        name = str(info.get("username") or "")
        if name != self.cfg.username:
            raise FreelancerError(
                f"logged out: expected {self.cfg.username!r}, got {name!r} "
                "(cookies expired -- re-export from the browser)"
            )
        if str(info.get("id") or "") != str(self.uid):
            raise FreelancerError(f"user id mismatch: {info.get('id')} != {self.uid}")
        return name

    def fetch_projects(self, term: str, limit: int = 100) -> list[dict]:
        data = self._get(
            "/projects/0.1/projects/active/",
            **{
                "full_description": "true",
                "job_details": "true",
                "bid_stats": "true",
                "limit": limit,
                "sort": "time_updated",
                "offset": 0,
                "query": term,
            },
        )
        return data.get("result", {}).get("projects", [])

    def project(self, pid: int) -> dict:
        """Single project. The endpoint returns result as the project dict itself, not a list."""
        data = self._get(
            f"/projects/0.1/projects/{pid}/",
            full_description="true",
            job_details=True,
            bid_stats="true",
            user_details="true",
        )
        res = data["result"]
        if isinstance(res, dict) and "projects" in res:
            return res["projects"][0]
        return res

    def my_bids(self, limit: int = 20) -> tuple[list[dict], int | None]:
        data = self._get("/projects/0.1/bids/", **{"bidders[]": self.uid, "limit": limit})
        res = data.get("result", {})
        return res.get("bids", []), res.get("bids_total_count")

    def project_bids(self, pid: int, limit: int = 20) -> list[dict]:
        data = self._get("/projects/0.1/bids/", **{"projects[]": pid, "limit": limit})
        return data.get("result", {}).get("bids", [])

    # --------------------------------------------------------------- write
    def place_bid(self, pid: int, amount: float, period: int, description: str,
                  milestone: int | None = None) -> dict:
        payload = {
            "project_id": int(pid),
            "bidder_id": int(self.uid),
            "amount": float(amount),
            "period": int(period),
            "milestone_percentage": int(self.cfg.milestone if milestone is None else milestone),
            "description": description,
        }
        url = f"{self.cfg.base}/projects/0.1/bids/?{COMMON}"
        r = self.s.post(url, json=payload, timeout=60)
        try:
            data = r.json()
        except ValueError:
            raise FreelancerError(f"non-JSON response ({r.status_code})", status=r.status_code)
        if data.get("status") != "success":
            raise FreelancerError(
                data.get("message", "bid rejected"),
                code=(data.get("error") or {}).get("code", ""),
                status=r.status_code,
            )
        return data["result"]
