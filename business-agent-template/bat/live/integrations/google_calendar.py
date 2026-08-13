"""Google Calendar adapter.

Reached only through the gate. Writing here puts an entry in the diary a real technician
will drive to, so the caller is expected to have checked `is_live()` first.

Adapted from the sibling `aiphone` project, with three changes:

- **Synchronous.** Everything in this codebase is; `asyncio.to_thread` around a blocking
  client had nothing to gain here.
- **Business hours come from `business_rules.yaml`**, not constants. Hours were hardcoded
  at 8 and 18 there, which is right until the day they change and one of the two copies
  does not.
- **Free/busy and booking are separate concerns.** The simulator remains the authority on
  which slots exist; this only reports what the real diary already has in it, so a booking
  made by a person on their phone still blocks the slot.

Credentials are a Google service account with the calendar shared to it. The service
account file path or its JSON goes in .env; the calendar id is `GOOGLE_CALENDAR_ID`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from bat.live.integrations.gate import LiveToolUnavailable, require_env

_SCOPES = ["https://www.googleapis.com/auth/calendar"]
_service: Any = None


def _client() -> Any:
    """Build the API client once. Import inside, so the library is only needed when live."""
    global _service
    if _service is not None:
        return _service
    try:
        from google.oauth2 import service_account  # noqa: PLC0415
        from googleapiclient.discovery import build  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on the deployment
        raise LiveToolUnavailable(
            "google-api-python-client and google-auth are not installed. "
            "The calendar is marked live but cannot be reached."
        ) from exc

    env = require_env("GOOGLE_CALENDAR_ID")
    import os  # noqa: PLC0415

    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if raw_json:
        credentials = service_account.Credentials.from_service_account_info(
            json.loads(raw_json), scopes=_SCOPES
        )
    elif path:
        credentials = service_account.Credentials.from_service_account_file(path, scopes=_SCOPES)
    else:
        raise LiveToolUnavailable(
            "Set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS in .env. "
            f"Calendar {env['GOOGLE_CALENDAR_ID']} cannot be reached without a service account."
        )
    _service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    return _service


def _calendar_id() -> str:
    return require_env("GOOGLE_CALENDAR_ID")["GOOGLE_CALENDAR_ID"]


def busy_periods(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """What the real diary already has between two times.

    Includes anything a person put there by hand. A slot search that only knows about our
    own bookings will happily send a technician to two places at once.
    """
    try:
        result = (
            _client()
            .freebusy()
            .query(
                body={
                    "timeMin": start.isoformat(),
                    "timeMax": end.isoformat(),
                    "items": [{"id": _calendar_id()}],
                }
            )
            .execute()
        )
    except LiveToolUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise LiveToolUnavailable(f"Calendar free/busy failed: {exc}") from exc

    busy = result.get("calendars", {}).get(_calendar_id(), {}).get("busy", [])
    return [
        (datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])) for b in busy
    ]


def create_event(
    *,
    start: datetime,
    duration_minutes: int,
    summary: str,
    description: str = "",
    location: str = "",
) -> str:
    """Put the booking in the diary. Returns the event id, which must be stored.

    Without that id a reschedule cannot move the entry and a cancellation cannot remove it,
    so the technician keeps driving to a job that is not happening.
    """
    body = {
        "summary": summary,
        "description": description,
        "location": location,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(minutes=duration_minutes)).isoformat()},
    }
    try:
        created = _client().events().insert(calendarId=_calendar_id(), body=body).execute()
    except LiveToolUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise LiveToolUnavailable(f"Calendar booking failed: {exc}") from exc
    return created.get("id", "")


def move_event(event_id: str, *, start: datetime, duration_minutes: int) -> None:
    """Move an existing entry. A reschedule that leaves the old one is a double booking."""
    if not event_id:
        raise LiveToolUnavailable(
            "No calendar event id for this appointment, so it cannot be moved. The booking "
            "was probably made while the calendar was not live."
        )
    try:
        _client().events().patch(
            calendarId=_calendar_id(),
            eventId=event_id,
            body={
                "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": (start + timedelta(minutes=duration_minutes)).isoformat()},
            },
        ).execute()
    except LiveToolUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise LiveToolUnavailable(f"Calendar reschedule failed: {exc}") from exc


def delete_event(event_id: str) -> None:
    """Remove the entry. An already-deleted event is not an error worth surfacing."""
    if not event_id:
        return
    try:
        _client().events().delete(calendarId=_calendar_id(), eventId=event_id).execute()
    except LiveToolUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        if "410" in str(exc) or "notFound" in str(exc):
            return
        raise LiveToolUnavailable(f"Calendar cancellation failed: {exc}") from exc
