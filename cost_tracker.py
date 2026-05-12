"""
Veritas IDV — API Cost Tracker
Logs per-transaction costs to Redis sorted sets and a local JSONL audit log.

Cost model:
  PASS API   (South Korea)  → fixed EUR per call (config.pass_api_cost_eur)
  Laser ID   (Thailand)     → fixed EUR per call (config.laser_id_cost_eur)
  VAV System (internal)     → 0.00 € "Internal Compute Cost"
  MRZ Fast Path             → 0.00 € (no external call)
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

import redis as redis_client

from config import settings

logger = logging.getLogger(__name__)

_redis = redis_client.from_url(settings.redis_url, decode_responses=True)

# JSONL audit log in project root
_LOG_FILE = Path(__file__).parent / "cost_audit.jsonl"

# Redis keys
_COST_TOTAL_KEY = "veritas:cost:total_eur"          # running float sum
_COST_EVENTS_KEY = "veritas:cost:events"             # list of last N JSON entries
_STATS_KEY = "veritas:stats:counts"                  # hash: success/fail/vav/fast
_MAX_EVENTS = 1000                                   # ring-buffer size in Redis

CostType = Literal["pass_api", "laser_id", "vav_system", "mrz_fast_path", "nfc"]


def _cost_for(cost_type: CostType) -> float:
    return {
        "pass_api":      settings.pass_api_cost_eur,
        "laser_id":      settings.laser_id_cost_eur,
        "vav_system":    settings.vav_compute_cost_eur,
        "mrz_fast_path": 0.00,
        "nfc":           0.00,
    }.get(cost_type, 0.00)


def log_transaction(
    *,
    task_id: str,
    project: str,
    country: str,
    cost_type: CostType,
    status: Literal["success", "failed", "manual_review"],
    extra: Optional[dict] = None,
) -> None:
    """Record one verification transaction and its associated cost."""
    cost_eur = _cost_for(cost_type)
    ts = datetime.now(timezone.utc).isoformat()

    entry = {
        "ts":        ts,
        "task_id":   task_id,
        "project":   project,
        "country":   country,
        "cost_type": cost_type,
        "cost_eur":  cost_eur,
        "status":    status,
        **(extra or {}),
    }

    try:
        # Running total
        _redis.incrbyfloat(_COST_TOTAL_KEY, cost_eur)

        # Ring-buffer of recent events (LPUSH + LTRIM keeps newest N)
        _redis.lpush(_COST_EVENTS_KEY, json.dumps(entry))
        _redis.ltrim(_COST_EVENTS_KEY, 0, _MAX_EVENTS - 1)

        # Aggregate counters
        pipe = _redis.pipeline()
        pipe.hincrby(_STATS_KEY, "total", 1)
        if status == "success":
            pipe.hincrby(_STATS_KEY, "success", 1)
        elif status == "failed":
            pipe.hincrby(_STATS_KEY, "failed", 1)
        else:
            pipe.hincrby(_STATS_KEY, "manual_review", 1)

        if cost_type == "vav_system":
            pipe.hincrby(_STATS_KEY, "vav_path", 1)
        else:
            pipe.hincrby(_STATS_KEY, "fast_path", 1)
        pipe.execute()

        # JSONL audit file (append-only, survives Redis restart)
        with _LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    except Exception:
        logger.exception("cost_tracker: failed to record transaction %s", task_id)


def get_stats() -> dict:
    """Return aggregate stats dict for the dashboard."""
    try:
        counts = _redis.hgetall(_STATS_KEY) or {}
        total_eur = float(_redis.get(_COST_TOTAL_KEY) or 0)
        total      = int(counts.get("total", 0))
        success    = int(counts.get("success", 0))
        failed     = int(counts.get("failed", 0))
        manual     = int(counts.get("manual_review", 0))
        vav_path   = int(counts.get("vav_path", 0))
        fast_path  = int(counts.get("fast_path", 0))

        vav_rate = round(vav_path / total * 100, 1) if total > 0 else 0.0

        return {
            "total":        total,
            "success":      success,
            "failed":       failed,
            "manual_review": manual,
            "vav_path":     vav_path,
            "fast_path":    fast_path,
            "vav_rate_pct": vav_rate,
            "total_cost_eur": round(total_eur, 4),
        }
    except Exception:
        logger.exception("cost_tracker: failed to read stats")
        return {
            "total": 0, "success": 0, "failed": 0, "manual_review": 0,
            "vav_path": 0, "fast_path": 0, "vav_rate_pct": 0.0, "total_cost_eur": 0.0,
        }


def get_recent_events(n: int = 20) -> list[dict]:
    """Return the N most recent cost events from the Redis ring-buffer."""
    try:
        raw = _redis.lrange(_COST_EVENTS_KEY, 0, n - 1)
        return [json.loads(r) for r in raw]
    except Exception:
        return []
