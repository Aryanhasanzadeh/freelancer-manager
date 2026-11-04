"""Runtime settings, editable from the menu instead of from .env + restart.

Everything here is deliberately a small, typed list: the menu can only offer knobs the
bot actually reads, and every change is visible in /status.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

KEY = "runtime"

# key -> (type, env fallback, default, label)
SPEC: dict[str, tuple] = {
    "budget_min": (float, "FL_BUDGET_MIN", 10.0, "کف بودجه (دلار)"),
    "budget_max": (float, "FL_BUDGET_MAX", 800.0, "سقف بودجه (دلار)"),
    "max_bids": (int, "FL_MAX_BIDS", 60, "حداکثر پیشنهاد رقیب"),
    "shortlist": (int, "FL_SHORTLIST", 3, "تعداد پروژه در هر اسکن"),
    "interval": (int, "FL_INTERVAL", 30, "فاصلهٔ اسکن (دقیقه)"),
    "period": (int, "FL_PERIOD", 7, "مدت تحویل پیش‌فرض (روز)"),
    "milestone": (int, "FL_MILESTONE", 100, "درصد milestone"),
    "scout": (bool, None, True, "اسکن خودکار پس‌زمینه"),
}


@dataclass
class Runtime:
    values: dict = field(default_factory=dict)
    store: object = None

    # ---------------------------------------------------------------- load
    @classmethod
    def load(cls, store) -> "Runtime":
        vals: dict = {}
        for key, (typ, env, default, _label) in SPEC.items():
            raw = os.environ.get(env, "") if env else ""
            if typ is bool:
                vals[key] = (raw.lower() in ("1", "true", "yes")) if raw else default
            else:
                try:
                    vals[key] = typ(raw) if raw else typ(default)
                except ValueError:
                    vals[key] = typ(default)
        saved = store.get_meta(KEY, None) if store else None
        if isinstance(saved, dict):
            for key, (typ, _e, _d, _l) in SPEC.items():
                if key in saved:
                    try:
                        vals[key] = typ(saved[key])
                    except (TypeError, ValueError):
                        pass
        rt = cls(values=vals, store=store)
        rt.normalize()
        return rt

    def normalize(self) -> None:
        v = self.values
        v["budget_min"] = max(0.0, float(v["budget_min"]))
        v["budget_max"] = max(v["budget_min"], float(v["budget_max"]))
        v["max_bids"] = max(0, int(v["max_bids"]))
        v["shortlist"] = max(1, int(v["shortlist"]))
        v["interval"] = max(0, int(v["interval"]))
        v["period"] = max(1, int(v["period"]))
        v["milestone"] = min(100, max(1, int(v["milestone"])))

    def save(self) -> None:
        if self.store:
            self.store.set_meta(KEY, self.values)

    # ----------------------------------------------------------------- get
    def __getitem__(self, key: str):
        return self.values[key]

    def set(self, key: str, raw: str) -> str:
        """Validate one typed value; returns the stored value or raises ValueError."""
        typ = SPEC[key][0]
        if typ is bool:
            low = raw.strip().lower()
            if low in ("1", "on", "true", "روشن", "بله"):
                self.values[key] = True
            elif low in ("0", "off", "false", "خاموش", "نه"):
                self.values[key] = False
            else:
                raise ValueError("مقدار باید روشن/خاموش باشد")
        elif typ is float:
            self.values[key] = float(raw)
        else:
            self.values[key] = int(raw)
        self.normalize()
        self.save()
        return str(self.values[key])

    def summary(self) -> str:
        out = []
        for key, (_t, _e, _d, label) in SPEC.items():
            val = self.values[key]
            if isinstance(val, bool):
                val = "روشن ✅" if val else "خاموش ⛔️"
            elif key in ("budget_min", "budget_max"):
                val = f"${val:g}"
            out.append(f"{label}: <b>{val}</b>")
        return "\n".join(out)

    def apply(self, cfg) -> None:
        """Push to the live Config so the next scan picks it up without a restart."""
        cfg.budget_min = self.values["budget_min"]
        cfg.budget_max = self.values["budget_max"]
        cfg.max_bids = self.values["max_bids"]
        cfg.shortlist = self.values["shortlist"]
        cfg.default_period = self.values["period"]
        cfg.milestone = self.values["milestone"]
