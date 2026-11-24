"""Read-only derived cargo statuses; never mutate delivery state or call HTTP."""
from datetime import date

from delivery import advertio_cargo_catalog as catalog

BLOCKED_STATES = ("incomplete", "catalog_unavailable", "catalog_refresh_pending",
                  "unsupported_route", "ambiguous_route",
                  "expired", "stale", "invalid_date")


def diagnose(snapshot, *, status, departure, posted, origin_country, origin_city,
             destination_country, destination_city, evidence, next_attempt,
             today, cutoff, now):
    """Return (display status, actionable reason) for a locally processed row."""
    if status not in ("waiting", "retry"):
        return status, None
    departure = str(departure or "").strip()
    if departure:
        try:
            trip = date.fromisoformat(departure[:10])
        except ValueError:
            return "invalid_date", "Invalid Gregorian departure date"
        if trip.isoformat() < today:
            return "expired", "Expired departure date"
    elif str(posted or "")[:10] < cutoff:
        return "stale", "Outside undated backfill window"
    sides = (("origin", origin_country, origin_city),
             ("destination", destination_country, destination_city))
    missing = [f"{side} {field}" for side, country, city in sides
               for field, value in (("country", country), ("city", city))
               if not str(value or "").strip()]
    if missing:
        return "incomplete", "Missing " + ", ".join(missing)
    if snapshot is None:
        return "catalog_unavailable", "Catalog unavailable; successful sync required"
    failures = []
    for side, country, city in sides:
        _, reason, code = snapshot.resolve(side, country, city, evidence)
        if reason:
            failures.append((reason, f"{side}: {reason} ({code or country}/{city})"))
    if failures:
        details = "; ".join(detail for _, detail in failures)
        if snapshot.stale:
            return "catalog_refresh_pending", "Catalog refresh pending: " + details
        state = "ambiguous_route" if any(reason == "ambiguous_city" for reason, _ in failures) else "unsupported_route"
        return state, details
    if next_attempt and next_attempt > now:
        return status, "Retry not due until " + next_attempt
    return status, None


def register(conn, snapshot, *, today, cutoff, now):
    """Register report-only SQL functions over the same snapshot as queue admission."""
    def evaluate(status, departure, posted, origin_country, origin_city,
                 destination_country, destination_city, evidence, next_attempt):
        return diagnose(snapshot, status=status, departure=departure, posted=posted,
                        origin_country=origin_country, origin_city=origin_city,
                        destination_country=destination_country, destination_city=destination_city,
                        evidence=evidence, next_attempt=next_attempt,
                        today=today, cutoff=cutoff, now=now)
    conn.create_function("cargo_display_status", 9, lambda *args: evaluate(*args)[0], deterministic=True)
    conn.create_function("cargo_block_reason", 9, lambda *args: evaluate(*args)[1], deterministic=True)


def sql_args(state, next_attempt):
    return (f"{state},t.departure_date,m.date,t.origin_country,t.origin_city,"
            f"t.destination_country,t.destination_city,{catalog.ROUTE_EVIDENCE_SQL},{next_attempt}")
