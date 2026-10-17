"""Persistent subtitle eligibility; scheduling never weakens validation."""
from datetime import datetime, timedelta, timezone


def utcnow():
    return datetime.now(timezone.utc)


def eligible(details, now=None):
    value = details.get("next_eligible_attempt")
    if not value:
        return True
    try:
        boundary = datetime.fromisoformat(value)
        if boundary.tzinfo is None:
            boundary = boundary.replace(tzinfo=timezone.utc)
        return (now or utcnow()) >= boundary
    except (TypeError, ValueError):
        return True


def waiting_details(previous, details, cfg, *, provider=False, now=None):
    now = now or utcnow()
    hours = max(1.0, float(cfg.get("provider_backoff_hours" if provider else "retry_cooldown_hours", 24 if provider else 168)))
    reason = details.get("reason") or details.get("error") or "No suitable subtitle"
    history = list(previous.get("attempt_history") or [])[-19:]
    history.append({"at": now.isoformat(), "reason": reason, "kind": "provider" if provider else "content"})
    boundary = now + timedelta(hours=hours)
    if details.get("release_eligible_date"):
        boundary = max(boundary, datetime.fromisoformat(details["release_eligible_date"]).replace(tzinfo=timezone.utc))
    return {**details, "reason": reason, "last_attempt": now.isoformat(),
            "next_eligible_attempt": boundary.isoformat(),
            "retry_reason": "provider_backoff" if provider else "suitable_subtitle_unavailable",
            "attempt_history": history}
