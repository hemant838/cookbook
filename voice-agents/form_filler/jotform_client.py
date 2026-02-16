"""Jotform integration — submit voice-collected form data as native Jotform submissions.

How it works:
1. You create a form in Jotform with fields matching your FormEngine labels
2. This client auto-discovers the question IDs by matching field labels
3. On form confirm, it POSTs a submission → appears in Jotform dashboard instantly

Env vars:
    JOTFORM_API_KEY  — your Jotform API key (Settings → API)
    JOTFORM_FORM_ID  — numeric form ID (from the form URL)
"""

import os
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

JOTFORM_BASE_URL = "https://api.jotform.com"


class JotformClient:
    """Sync-free Jotform client for submitting form data."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        form_id: Optional[str] = None,
    ):
        self.api_key = api_key or os.getenv("JOTFORM_API_KEY", "")
        self.form_id = form_id or os.getenv("JOTFORM_FORM_ID", "")
        self._question_map: Dict[str, str] = {}  # label → question ID

        if not self.api_key or not self.form_id:
            logger.warning(
                "[Jotform] JOTFORM_API_KEY or JOTFORM_FORM_ID not set — "
                "submissions will be skipped"
            )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.form_id)

    # ------------------------------------------------------------------
    # Auto-discover question IDs from the Jotform form
    # ------------------------------------------------------------------

    async def discover_questions(self) -> Dict[str, str]:
        """Fetch form questions and build a label → question_id map.

        Jotform question objects look like:
            {"1": {"name": "fullName", "text": "Full Name", "type": "control_fullname", ...}}

        We match on the 'text' field (the label visible in the form).
        """
        if not self.enabled:
            return {}

        url = f"{JOTFORM_BASE_URL}/form/{self.form_id}/questions"
        params = {"apiKey": self.api_key}

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()

        data = resp.json()
        questions = data.get("content", {})

        self._question_map = {}
        for qid, q in questions.items():
            label = q.get("text", "").strip()
            if label:
                # Store both exact and lowercase for flexible matching
                self._question_map[label.lower()] = qid
                logger.debug(f"[Jotform] Q{qid}: {label} ({q.get('type', '?')})")

        logger.info(
            f"[Jotform] Discovered {len(self._question_map)} questions "
            f"from form {self.form_id}"
        )
        return self._question_map

    # ------------------------------------------------------------------
    # Submit form data
    # ------------------------------------------------------------------

    async def submit(
        self,
        form_data: Dict[str, Any],
        field_labels: Dict[str, str],
    ) -> Dict[str, Any]:
        """Submit collected data as a Jotform form submission.

        Args:
            form_data: Dict of {field_name: value} from FormEngine.data
            field_labels: Dict of {field_name: label} for matching to Jotform questions

        Returns:
            Jotform API response with submission ID and URL.
        """
        if not self.enabled:
            return {"status": "skipped", "reason": "Jotform not configured"}

        # Auto-discover if we haven't yet
        if not self._question_map:
            await self.discover_questions()

        # Build submission payload: submission[qid] = value
        submission: Dict[str, str] = {}
        matched = 0
        unmatched: List[str] = []

        for field_name, value in form_data.items():
            label = field_labels.get(field_name, field_name)
            qid = self._question_map.get(label.lower())

            if qid:
                submission[f"submission[{qid}]"] = str(value)
                matched += 1
            else:
                unmatched.append(f"{field_name} ({label})")

        if unmatched:
            logger.warning(
                f"[Jotform] {len(unmatched)} fields not matched to Jotform questions: "
                f"{', '.join(unmatched)}"
            )

        if not submission:
            return {
                "status": "error",
                "reason": "No fields matched Jotform questions",
            }

        # POST the submission
        url = f"{JOTFORM_BASE_URL}/form/{self.form_id}/submissions"
        submission["apiKey"] = self.api_key

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, data=submission)
                resp.raise_for_status()

            result = resp.json()
            submission_id = result.get("content", {}).get("submissionID", "?")

            logger.success(
                f"[Jotform] Submission created! ID: {submission_id} "
                f"({matched} fields matched)"
            )

            return {
                "status": "submitted",
                "submission_id": submission_id,
                "fields_matched": matched,
                "fields_unmatched": len(unmatched),
                "view_url": f"https://www.jotform.com/submission/{submission_id}",
            }

        except httpx.HTTPStatusError as e:
            logger.error(f"[Jotform] API error: {e.response.status_code} — {e.response.text}")
            return {"status": "error", "code": e.response.status_code, "detail": e.response.text}
        except Exception as e:
            logger.error(f"[Jotform] Failed: {e}")
            return {"status": "error", "detail": str(e)}
