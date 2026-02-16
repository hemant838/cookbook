"""Form Engine — state machine for structured voice data collection.

Defines form schemas with typed fields, validation rules, and step-by-step
progression.  The agent follows the state machine — the LLM handles
conversation, the engine handles structure.

Supports:
- Field validation (regex, choices, numeric ranges)
- Step-by-step progression with backtracking
- Review and confirmation flow
- HTML report generation at completion
"""

import json
import os
import re
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from loguru import logger


# ---------------------------------------------------------------------------
# Field + Step definitions
# ---------------------------------------------------------------------------

class FieldType(str, Enum):
    TEXT = "text"
    NUMBER = "number"
    DATE = "date"         # YYYY-MM-DD
    PHONE = "phone"
    EMAIL = "email"
    CHOICE = "choice"     # one of a set
    CURRENCY = "currency" # numeric amount in ₹


class FormField:
    """A single form field with validation."""

    def __init__(
        self,
        name: str,
        label: str,
        field_type: FieldType,
        required: bool = True,
        choices: Optional[List[str]] = None,
        pattern: Optional[str] = None,
        min_val: Optional[float] = None,
        max_val: Optional[float] = None,
        hint: str = "",
    ):
        self.name = name
        self.label = label
        self.field_type = field_type
        self.required = required
        self.choices = choices
        self.pattern = re.compile(pattern) if pattern else None
        self.min_val = min_val
        self.max_val = max_val
        self.hint = hint

    def validate(self, value: str) -> tuple[bool, str, str]:
        """Validate and normalize a field value.

        Returns (is_valid, error_message, normalized_value).
        The normalized value handles voice-friendly corrections:
        - Choices: "top up" → "Top-up" (canonical form)
        - Patterns: case-insensitive matching then uppercased
        - Phone: digit extraction
        """
        if not value and self.required:
            return False, f"{self.label} is required", value

        if not value:
            return True, "", value

        if self.field_type == FieldType.NUMBER or self.field_type == FieldType.CURRENCY:
            try:
                num = float(value.replace(",", "").replace("₹", "").strip())
                if self.min_val is not None and num < self.min_val:
                    return False, f"{self.label} must be at least {self.min_val}", value
                if self.max_val is not None and num > self.max_val:
                    return False, f"{self.label} must be at most {self.max_val}", value
            except ValueError:
                return False, f"{self.label} must be a number", value

        if self.field_type == FieldType.DATE:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                return False, f"{self.label} must be a valid date (YYYY-MM-DD)", value

        if self.field_type == FieldType.PHONE:
            digits = re.sub(r"[^\d]", "", value)
            if len(digits) < 10 or len(digits) > 13:
                return False, f"{self.label} must be a valid phone number (10-13 digits)", value
            # Normalize to just digits
            value = digits

        if self.field_type == FieldType.EMAIL:
            if not re.match(r"[^@]+@[^@]+\.[^@]+", value):
                return False, f"{self.label} must be a valid email address", value

        if self.choices:
            # Voice-friendly: normalize hyphens, spaces, case for comparison
            def _norm(s: str) -> str:
                return re.sub(r"[\s\-_]+", "", s).lower()

            norm_value = _norm(value)
            matched = None
            for c in self.choices:
                if _norm(c) == norm_value:
                    matched = c
                    break
            if not matched:
                return False, f"{self.label} must be one of: {', '.join(self.choices)}", value
            # Use canonical form (e.g. "top up" → "Top-up")
            value = matched

        if self.pattern:
            # Voice-friendly: try case-insensitive match, then uppercase the result
            if not self.pattern.match(value) and not self.pattern.match(value.upper()):
                return False, f"{self.label} format is invalid", value
            # Store uppercase version if lowercase matched
            if not self.pattern.match(value):
                value = value.upper()

        return True, "", value


class FormStep:
    """A group of related fields collected together."""

    def __init__(self, name: str, title: str, description: str, fields: List[FormField]):
        self.name = name
        self.title = title
        self.description = description
        self.fields = fields

    def get_field_names(self) -> List[str]:
        return [f.name for f in self.fields]

    def get_prompt_instructions(self) -> str:
        """Generate instructions for the LLM about this step's fields.

        Includes the field_name identifier so the LLM knows exactly what
        to pass to set_field().
        """
        lines = [f"**Step: {self.title}**", self.description, "Fields to collect:"]
        for f in self.fields:
            req = "required" if f.required else "optional"
            hint = f" ({f.hint})" if f.hint else ""
            choices = f" — options: {', '.join(f.choices)}" if f.choices else ""
            lines.append(
                f'  - "{f.name}": {f.label} [{f.field_type.value}, {req}]{hint}{choices}'
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# State Machine
# ---------------------------------------------------------------------------

class FormState(str, Enum):
    NOT_STARTED = "not_started"
    COLLECTING = "collecting"
    REVIEWING = "reviewing"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class FormEngine:
    """State machine that drives form collection.

    The agent calls tools on this engine. The engine tracks:
    - Current step
    - Collected values per field
    - Validation state
    - Overall form progress
    """

    def __init__(self, form_name: str, steps: List[FormStep]):
        self.form_name = form_name
        self.steps = steps
        self.state = FormState.NOT_STARTED
        self.current_step_idx = 0
        self.data: Dict[str, Any] = {}
        self.errors: Dict[str, str] = {}
        self._started_at: Optional[str] = None
        self._completed_at: Optional[str] = None

    @property
    def current_step(self) -> Optional[FormStep]:
        if 0 <= self.current_step_idx < len(self.steps):
            return self.steps[self.current_step_idx]
        return None

    @property
    def progress(self) -> Dict:
        """Current form progress."""
        total_fields = sum(len(s.fields) for s in self.steps)
        filled = sum(1 for v in self.data.values() if v)
        return {
            "state": self.state.value,
            "current_step": self.current_step.title if self.current_step else "Done",
            "step_number": self.current_step_idx + 1,
            "total_steps": len(self.steps),
            "fields_filled": filled,
            "total_fields": total_fields,
            "percent": round(filled / total_fields * 100) if total_fields else 0,
        }

    def start(self) -> Dict:
        """Start the form collection."""
        self.state = FormState.COLLECTING
        self._started_at = datetime.now().isoformat()
        step = self.current_step
        return {
            "status": "started",
            "step": step.get_prompt_instructions() if step else "",
            **self.progress,
        }

    def set_field(self, field_name: str, value: str) -> Dict:
        """Set a field value with validation.

        Returns validation result and next instructions.
        """
        # Find the field
        field = None
        for step in self.steps:
            for f in step.fields:
                if f.name == field_name:
                    field = f
                    break

        if not field:
            return {"success": False, "error": f"Unknown field: {field_name}"}

        # Validate and normalize (voice-friendly corrections)
        is_valid, error, normalized = field.validate(value)
        if not is_valid:
            self.errors[field_name] = error
            return {"success": False, "error": error, "field": field_name}

        # Store the normalized value (e.g. "top up" → "Top-up")
        self.data[field_name] = normalized
        if field_name in self.errors:
            del self.errors[field_name]

        return {
            "success": True,
            "field": field_name,
            "value": value,
            **self.progress,
        }

    def next_step(self) -> Dict:
        """Move to the next step if current step is complete."""
        step = self.current_step
        if not step:
            return {"status": "error", "message": "No current step"}

        # Check all required fields in current step are filled
        missing = []
        for f in step.fields:
            if f.required and f.name not in self.data:
                missing.append(f.label)

        if missing:
            return {
                "status": "incomplete",
                "missing_fields": missing,
                "message": f"Please provide: {', '.join(missing)}",
            }

        # Advance
        self.current_step_idx += 1

        if self.current_step_idx >= len(self.steps):
            self.state = FormState.REVIEWING
            return {
                "status": "review",
                "message": "All fields collected! Please review the form.",
                "data": self.data,
                **self.progress,
            }

        next_step = self.current_step
        return {
            "status": "next_step",
            "step": next_step.get_prompt_instructions() if next_step else "",
            **self.progress,
        }

    def previous_step(self) -> Dict:
        """Go back to the previous step."""
        if self.current_step_idx > 0:
            self.current_step_idx -= 1
            self.state = FormState.COLLECTING
            step = self.current_step
            return {
                "status": "previous_step",
                "step": step.get_prompt_instructions() if step else "",
                **self.progress,
            }
        return {"status": "error", "message": "Already at the first step"}

    def get_review(self) -> Dict:
        """Get all collected data for review."""
        review_lines = []
        for step in self.steps:
            review_lines.append(f"\n--- {step.title} ---")
            for f in step.fields:
                val = self.data.get(f.name, "(not provided)")
                review_lines.append(f"  {f.label}: {val}")
        return {
            "status": "review",
            "summary": "\n".join(review_lines),
            "data": self.data,
        }

    def confirm(self) -> Dict:
        """Confirm and finalize the form."""
        self.state = FormState.CONFIRMED
        self._completed_at = datetime.now().isoformat()
        return {
            "status": "confirmed",
            "data": self.data,
            "started_at": self._started_at,
            "completed_at": self._completed_at,
        }

    # ------------------------------------------------------------------
    # HTML report generation
    # ------------------------------------------------------------------

    def generate_html_report(self, output_dir: str = ".") -> str:
        """Generate a beautiful HTML report of the completed form.

        Returns the file path.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self.form_name.lower().replace(' ', '_')}_{timestamp}.html"
        filepath = os.path.join(output_dir, filename)

        sections_html = ""
        for step in self.steps:
            fields_html = ""
            for f in step.fields:
                val = self.data.get(f.name, "—")
                fields_html += f"""
                <div class="field">
                    <span class="label">{f.label}</span>
                    <span class="value">{val}</span>
                </div>"""
            sections_html += f"""
            <div class="section">
                <h2>{step.title}</h2>
                {fields_html}
            </div>"""

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{self.form_name}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f5f5f5;
            color: #1a1a1a;
            padding: 2rem;
        }}
        .container {{
            max-width: 720px;
            margin: 0 auto;
            background: white;
            border-radius: 12px;
            box-shadow: 0 2px 12px rgba(0,0,0,0.08);
            overflow: hidden;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 2rem;
        }}
        .header h1 {{ font-size: 1.5rem; font-weight: 600; }}
        .header .meta {{
            margin-top: 0.5rem;
            font-size: 0.85rem;
            opacity: 0.9;
        }}
        .section {{
            padding: 1.5rem 2rem;
            border-bottom: 1px solid #eee;
        }}
        .section:last-child {{ border-bottom: none; }}
        .section h2 {{
            font-size: 1rem;
            font-weight: 600;
            color: #667eea;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 1rem;
        }}
        .field {{
            display: flex;
            justify-content: space-between;
            padding: 0.6rem 0;
            border-bottom: 1px solid #f0f0f0;
        }}
        .field:last-child {{ border-bottom: none; }}
        .label {{
            color: #666;
            font-size: 0.9rem;
        }}
        .value {{
            font-weight: 500;
            text-align: right;
            max-width: 60%;
        }}
        .footer {{
            padding: 1.5rem 2rem;
            background: #fafafa;
            font-size: 0.8rem;
            color: #999;
            text-align: center;
        }}
        .badge {{
            display: inline-block;
            background: #e8f5e9;
            color: #2e7d32;
            padding: 0.25rem 0.75rem;
            border-radius: 20px;
            font-size: 0.8rem;
            font-weight: 500;
            margin-top: 0.5rem;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>{self.form_name}</h1>
            <div class="meta">
                Submitted: {datetime.now().strftime("%B %d, %Y at %I:%M %p")}
                <br><span class="badge">✓ Collected via Voice Agent</span>
            </div>
        </div>
        {sections_html}
        <div class="footer">
            Generated by Smallest AI Voice Agent &middot; Form collected via phone conversation
        </div>
    </div>
</body>
</html>"""

        with open(filepath, "w") as f:
            f.write(html)

        logger.info(f"[FormEngine] HTML report saved: {filepath}")
        return filepath

    def to_json(self) -> str:
        """Export form data as JSON."""
        return json.dumps({
            "form_name": self.form_name,
            "state": self.state.value,
            "data": self.data,
            "started_at": self._started_at,
            "completed_at": self._completed_at,
        }, indent=2, default=str)


# ---------------------------------------------------------------------------
# Pre-built form: Health Insurance Claim
# ---------------------------------------------------------------------------

INSURANCE_CLAIM_FORM = FormEngine(
    form_name="Health Insurance Claim",
    steps=[
        FormStep(
            name="personal",
            title="Personal Details",
            description="Collect the patient's personal information.",
            fields=[
                FormField("full_name", "Full Name", FieldType.TEXT,
                         hint="as on insurance policy"),
                FormField("date_of_birth", "Date of Birth", FieldType.DATE,
                         hint="convert spoken date to YYYY-MM-DD before submitting"),
                FormField("phone", "Phone Number", FieldType.PHONE,
                         hint="10-digit mobile number"),
                FormField("email", "Email Address", FieldType.EMAIL,
                         required=False, hint="for sending claim status updates"),
            ],
        ),
        FormStep(
            name="policy",
            title="Policy Details",
            description="Collect insurance policy information.",
            fields=[
                FormField("policy_number", "Policy Number", FieldType.TEXT,
                         pattern=r"^[A-Z]{2,4}-\d{6,10}$",
                         hint="e.g. HLT-12345678"),
                FormField("insurer_name", "Insurance Company", FieldType.TEXT,
                         hint="e.g. Star Health, ICICI Lombard"),
                FormField("plan_type", "Plan Type", FieldType.CHOICE,
                         choices=["Individual", "Family Floater", "Group", "Top-up"],
                         hint="type of health insurance plan"),
            ],
        ),
        FormStep(
            name="treatment",
            title="Treatment Details",
            description="Collect information about the treatment/hospitalization.",
            fields=[
                FormField("hospital_name", "Hospital Name", FieldType.TEXT),
                FormField("admission_date", "Date of Admission", FieldType.DATE,
                         hint="convert spoken date to YYYY-MM-DD before submitting"),
                FormField("discharge_date", "Date of Discharge", FieldType.DATE,
                         hint="convert spoken date to YYYY-MM-DD before submitting"),
                FormField("diagnosis", "Diagnosis / Condition", FieldType.TEXT,
                         hint="e.g. Dengue fever, Appendicitis"),
                FormField("treatment_type", "Treatment Type", FieldType.CHOICE,
                         choices=["Hospitalization", "Day Care", "OPD", "Maternity"],
                         hint="type of treatment"),
            ],
        ),
        FormStep(
            name="financial",
            title="Claim Amount",
            description="Collect the financial details of the claim.",
            fields=[
                FormField("total_bill_amount", "Total Hospital Bill (₹)", FieldType.CURRENCY,
                         min_val=100, max_val=50_00_000,
                         hint="total bill amount in rupees"),
                FormField("claim_amount", "Claim Amount (₹)", FieldType.CURRENCY,
                         min_val=100, max_val=50_00_000,
                         hint="amount being claimed"),
                FormField("bank_account_last4", "Bank Account (last 4 digits)", FieldType.TEXT,
                         pattern=r"^\d{4}$",
                         hint="for reimbursement transfer"),
            ],
        ),
    ],
)


def create_insurance_claim_form() -> FormEngine:
    """Create a fresh insurance claim form instance."""
    return FormEngine(
        form_name=INSURANCE_CLAIM_FORM.form_name,
        steps=[
            FormStep(
                name=step.name,
                title=step.title,
                description=step.description,
                fields=step.fields,
            )
            for step in INSURANCE_CLAIM_FORM.steps
        ],
    )
