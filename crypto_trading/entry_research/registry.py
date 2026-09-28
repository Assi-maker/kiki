"""Freeze the entry-quality registry from a research run.

    python -m crypto_trading.entry_research.registry

The registry is FROZEN: the shadow Entry Quality Layer only ever reads it, and
only candidates discovered after `frozen_at` count as forward out-of-sample
evidence. Re-freezing is an explicit human step (a new version), never
automatic - otherwise the forward test would be fitted to itself.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

REGISTRY_PATH = Path(__file__).resolve().parents[1] / "config" / "entry_quality_registry.json"
USED_CLASSES = {"EDGE", "WEAK_EDGE", "HYPOTHESIS", "FAILURE_PATTERN", "FAILURE_HYPOTHESIS"}


def _brief(s: dict | None) -> dict | None:
    if not s or not s.get("n"):
        return None
    return {k: s.get(k) for k in ("n", "mean_r", "median_r", "win_rate", "pf", "max_dd_r", "ci")}


def freeze(results: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    used, excluded = [], []
    for p in results["patterns"]:
        entry = {
            "id": f"{p['side']}-{len(used) + len(excluded):02d}", "label": p["pattern"], "side": p["side"],
            "class": p["class"], "conditions": p["conditions"],
            "evidence": {"train": _brief(p["train"]), "valid": _brief(p["valid"]), "test": _brief(p["test"]),
                         "train_family_q": p["train_family_q"], "valid_q": p["valid_q"], "test_q": p["test_q"]},
        }
        (used if p["class"] in USED_CLASSES else excluded).append(entry)
    return {
        "version": now.strftime("%Y%m%dT%H%MZ"), "frozen_at": now.isoformat(),
        "source": "crypto_trading.entry_research.run (results generated_at %s)" % results["generated_at"],
        "design": results["design"], "cuts": results["cuts"],
        "note": ("Shadow-only. No pattern here is a validated EDGE; FAILURE_HYPOTHESIS/HYPOTHESIS are "
                 "sample-limited hypotheses awaiting forward evidence (>= 30 forward observations)."),
        "patterns": used,
        "excluded": [{"label": e["label"], "class": e["class"], "side": e["side"]} for e in excluded],
    }


def load(path: Path = REGISTRY_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    results = json.loads(Path("data/entry_research/results.json").read_text(encoding="utf-8"))
    reg = freeze(results)
    REGISTRY_PATH.write_text(json.dumps(reg, indent=1, default=str), encoding="utf-8")
    print(REGISTRY_PATH, reg["version"], len(reg["patterns"]), "patterns used,", len(reg["excluded"]), "excluded")


if __name__ == "__main__":
    main()
