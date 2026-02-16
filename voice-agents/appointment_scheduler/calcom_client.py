"""Cal.com integration — check real calendar availability and book appointments.

How it works:
1. You set up event types in Cal.com (e.g. "General Consultation — 30 min")
2. This client queries Cal.com for available slots on any date
3. When the patient picks a slot, it creates a real booking → shows in Cal.com dashboard

Env vars:
    CAL_API_KEY      — your Cal.com API key (Settings → Developer → API Keys)
    CAL_EVENT_TYPE_ID — numeric event type ID (from the URL when editing an event type)
"""

import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
from loguru import logger

CAL_BASE_URL = "https://api.cal.com"
CAL_API_VERSION = "2024-08-13"


class CalcomClient:
    """Async Cal.com v2 client for slot checking and booking."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        event_type_id: Optional[str] = None,
    ):
        self.api_key = api_key or os.getenv("CAL_API_KEY", "")
        self.event_type_id = event_type_id or os.getenv("CAL_EVENT_TYPE_ID", "")
        self.timezone = os.getenv("CAL_TIMEZONE", "Asia/Kolkata")

        if not self.api_key or not self.event_type_id:
            logger.warning(
                "[Cal.com] CAL_API_KEY or CAL_EVENT_TYPE_ID not set — "
                "agent will not be able to check availability or book"
            )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.event_type_id)

    @property
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "cal-api-version": CAL_API_VERSION,
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Discover event types (optional — run once to find your event type ID)
    # ------------------------------------------------------------------

    async def discover_event_types(self) -> List[Dict[str, Any]]:
        """Fetch all event types from your Cal.com account.

        Useful for initial setup — run once to find your event type ID.
        Returns list of {id, title, slug, length} dicts.
        """
        if not self.api_key:
            return []

        url = f"{CAL_BASE_URL}/v2/event-types"

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, headers=self._headers)
                resp.raise_for_status()

            data = resp.json()
            event_types = data.get("data", [])

            result = []
            for et in event_types:
                info = {
                    "id": et.get("id"),
                    "title": et.get("title", ""),
                    "slug": et.get("slug", ""),
                    "length": et.get("length", 0),
                }
                result.append(info)
                logger.debug(
                    f"[Cal.com] Event type: {info['title']} "
                    f"(ID: {info['id']}, {info['length']}min)"
                )

            logger.info(f"[Cal.com] Found {len(result)} event types")
            return result

        except Exception as e:
            logger.error(f"[Cal.com] Failed to fetch event types: {e}")
            return []

    # ------------------------------------------------------------------
    # Get available slots
    # ------------------------------------------------------------------

    async def get_available_slots(
        self, date: str, count: int = 5
    ) -> Dict[str, Any]:
        """Get available time slots for a specific date.

        Args:
            date: Date in YYYY-MM-DD format.
            count: Max number of slots to return.

        Returns:
            Dict with "slots" list and metadata, or error info.
        """
        if not self.enabled:
            return {"status": "error", "reason": "Cal.com not configured"}

        # Query the full day — use local timezone boundaries
        local_tz = ZoneInfo(self.timezone)
        day_start = datetime.strptime(date, "%Y-%m-%d").replace(
            tzinfo=local_tz
        )
        day_end = day_start + timedelta(days=1)

        # Convert to UTC ISO strings for the API
        start_time = day_start.astimezone(ZoneInfo("UTC")).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        end_time = day_end.astimezone(ZoneInfo("UTC")).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )

        url = f"{CAL_BASE_URL}/v2/slots/available"
        params = {
            "startTime": start_time,
            "endTime": end_time,
            "eventTypeId": self.event_type_id,
            "timeZone": self.timezone,
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    url, headers=self._headers, params=params
                )
                resp.raise_for_status()

            data = resp.json()
            logger.debug(f"[Cal.com] Raw slots response: {data}")
            raw_slots = data.get("data", {}).get("slots", {})

            # Cal.com returns {date: [{time: "ISO"}, ...]}
            # Convert UTC times to local timezone for display
            slots = []
            for day, day_slots in raw_slots.items():
                for s in day_slots:
                    time_str = s.get("time", "")
                    if time_str:
                        # Parse UTC time and convert to local
                        dt_utc = datetime.fromisoformat(
                            time_str.replace("Z", "+00:00")
                        )
                        dt_local = dt_utc.astimezone(local_tz)
                        slots.append({
                            "start_time": dt_local.strftime("%H:%M"),
                            "iso": time_str,
                        })

            # Sort by time and limit results
            slots.sort(key=lambda s: s["start_time"])
            slots = slots[:count]

            logger.info(
                f"[Cal.com] Found {len(slots)} available slots on {date} "
                f"(tz={self.timezone})"
            )
            return {"status": "ok", "slots": slots, "count": len(slots)}

        except httpx.HTTPStatusError as e:
            logger.error(
                f"[Cal.com] Slots API error: {e.response.status_code} — "
                f"{e.response.text}"
            )
            return {
                "status": "error",
                "code": e.response.status_code,
                "detail": e.response.text,
            }
        except Exception as e:
            logger.error(f"[Cal.com] Failed to fetch slots: {e}")
            return {"status": "error", "detail": str(e)}

    # ------------------------------------------------------------------
    # Check specific slot
    # ------------------------------------------------------------------

    async def check_slot(
        self, date: str, time: str
    ) -> Dict[str, Any]:
        """Check if a specific time slot is available on Cal.com.

        Args:
            date: Date in YYYY-MM-DD format.
            time: Time in HH:MM 24-hour format (e.g. "17:00").

        Returns:
            Dict with availability status and alternatives if busy.
        """
        if not self.enabled:
            return {"status": "error", "reason": "Cal.com not configured"}

        # Get all slots for the day
        result = await self.get_available_slots(date, count=50)
        if result.get("status") != "ok":
            return result

        slots = result.get("slots", [])
        requested = time.strip()

        # Check if requested time is in available slots
        for slot in slots:
            if slot["start_time"] == requested:
                return {
                    "available": True,
                    "start_time": requested,
                    "iso": slot["iso"],
                    "source": "cal.com",
                }

        # Not available — find nearest alternatives
        try:
            req_minutes = int(requested.split(":")[0]) * 60 + int(
                requested.split(":")[1]
            )
        except ValueError:
            req_minutes = 0

        def distance(s):
            t = s["start_time"]
            m = int(t.split(":")[0]) * 60 + int(t.split(":")[1])
            return abs(m - req_minutes)

        alternatives = sorted(slots, key=distance)[:3]

        return {
            "available": False,
            "reason": f"Slot at {requested} is not available on Cal.com",
            "requested_time": requested,
            "alternatives": alternatives,
            "source": "cal.com",
        }

    # ------------------------------------------------------------------
    # Look up existing bookings
    # ------------------------------------------------------------------

    async def get_bookings(
        self, date: str = "", attendee_name: str = ""
    ) -> Dict[str, Any]:
        """Fetch existing bookings from Cal.com, optionally filtered by date and/or name.

        Args:
            date: Optional YYYY-MM-DD to filter bookings on that day.
            attendee_name: Optional name to filter (case-insensitive substring match).

        Returns:
            Dict with "bookings" list and count.
        """
        if not self.enabled:
            return {"status": "error", "reason": "Cal.com not configured"}

        local_tz = ZoneInfo(self.timezone)

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{CAL_BASE_URL}/v2/bookings",
                    headers=self._headers,
                    params={"status": "upcoming"},
                )
                resp.raise_for_status()

            data = resp.json()
            all_bookings = data.get("data", [])

            results = []
            name_lower = attendee_name.strip().lower()

            for b in all_bookings:
                # Parse start time → local
                start_str = b.get("start", "")
                if not start_str:
                    continue
                dt_utc = datetime.fromisoformat(
                    start_str.replace("Z", "+00:00")
                )
                dt_local = dt_utc.astimezone(local_tz)
                booking_date = dt_local.strftime("%Y-%m-%d")

                # Filter by date
                if date and booking_date != date:
                    continue

                # Filter by attendee name (fuzzy)
                attendees = b.get("attendees", [])
                attendee_names = [a.get("name", "") for a in attendees]
                if name_lower:
                    match = any(
                        name_lower in n.lower() for n in attendee_names
                    )
                    if not match:
                        continue

                results.append({
                    "booking_id": b.get("id"),
                    "date": booking_date,
                    "start_time": dt_local.strftime("%H:%M"),
                    "display_time": dt_local.strftime("%I:%M %p"),
                    "attendee": ", ".join(attendee_names) or "Unknown",
                    "title": b.get("title", ""),
                    "status": b.get("status", ""),
                })

            # Sort by date + time
            results.sort(key=lambda r: (r["date"], r["start_time"]))

            logger.info(
                f"[Cal.com] Found {len(results)} bookings"
                f"{f' on {date}' if date else ''}"
                f"{f' for {attendee_name}' if attendee_name else ''}"
            )
            return {
                "status": "ok",
                "bookings": results,
                "count": len(results),
            }

        except httpx.HTTPStatusError as e:
            logger.error(
                f"[Cal.com] Bookings API error: {e.response.status_code} — "
                f"{e.response.text}"
            )
            return {
                "status": "error",
                "code": e.response.status_code,
                "detail": e.response.text,
            }
        except Exception as e:
            logger.error(f"[Cal.com] Failed to fetch bookings: {e}")
            return {"status": "error", "detail": str(e)}

    # ------------------------------------------------------------------
    # Create booking
    # ------------------------------------------------------------------

    async def create_booking(
        self,
        date: str,
        time: str,
        attendee_name: str,
        attendee_email: str = "",
        reason: str = "",
    ) -> Dict[str, Any]:
        """Create a booking on Cal.com.

        Args:
            date: Date in YYYY-MM-DD format.
            time: Time in HH:MM 24-hour format.
            attendee_name: Full name of the patient/attendee.
            attendee_email: Email (required by Cal.com — uses placeholder if empty).
            reason: Reason for the appointment.

        Returns:
            Dict with booking confirmation or error.
        """
        if not self.enabled:
            return {"status": "error", "reason": "Cal.com not configured"}

        # First verify the slot is still available and get the ISO time
        check = await self.check_slot(date, time)
        if not check.get("available"):
            return {
                "success": False,
                "reason": check.get("reason", "Slot not available"),
                "alternatives": check.get("alternatives", []),
                "source": "cal.com",
            }

        # Build ISO start time — the iso from check_slot is already UTC
        iso_start = check.get("iso")
        if not iso_start:
            # Fallback: convert local time to UTC
            local_tz = ZoneInfo(self.timezone)
            local_dt = datetime.strptime(
                f"{date}T{time}", "%Y-%m-%dT%H:%M"
            ).replace(tzinfo=local_tz)
            iso_start = local_dt.astimezone(ZoneInfo("UTC")).strftime(
                "%Y-%m-%dT%H:%M:%S.000Z"
            )

        # Cal.com requires an email — use placeholder if not provided
        if not attendee_email:
            safe_name = attendee_name.lower().replace(" ", ".")
            attendee_email = f"{safe_name}@patient.local"

        url = f"{CAL_BASE_URL}/v2/bookings"
        body = {
            "eventTypeId": int(self.event_type_id),
            "start": iso_start,
            "attendee": {
                "name": attendee_name,
                "email": attendee_email,
                "timeZone": self.timezone,
            },
            "metadata": {
                "reason": reason,
                "source": "atoms-voice-agent",
            },
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    url, headers=self._headers, json=body
                )
                resp.raise_for_status()

            data = resp.json()
            booking = data.get("data", {})
            booking_id = booking.get("id") or booking.get("uid", "?")

            logger.success(
                f"[Cal.com] Booking created! ID: {booking_id} — "
                f"{attendee_name} at {time} on {date}"
            )

            return {
                "success": True,
                "source": "cal.com",
                "booking_id": booking_id,
                "appointment": {
                    "date": date,
                    "start_time": time,
                    "patient_name": attendee_name,
                    "reason": reason,
                    "title": booking.get("title", ""),
                },
                "cal_url": f"https://app.cal.com/bookings/{booking_id}",
            }

        except httpx.HTTPStatusError as e:
            logger.error(
                f"[Cal.com] Booking API error: {e.response.status_code} — "
                f"{e.response.text}"
            )
            return {
                "success": False,
                "reason": f"Cal.com API error: {e.response.status_code}",
                "detail": e.response.text,
                "source": "cal.com",
            }
        except Exception as e:
            logger.error(f"[Cal.com] Booking failed: {e}")
            return {"success": False, "reason": str(e), "source": "cal.com"}
