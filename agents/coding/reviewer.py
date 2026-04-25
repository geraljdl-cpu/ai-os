"""
agents/coding/reviewer.py — Review engineer output before execution.

Produces approve/reject with explicit reasons.
Rejects risky, behavior-changing, or out-of-scope changes.
Uses the review model from router config.
"""
import json
import os
import pathlib
import textwrap
import urllib.request
import urllib.error

from agents.coding.router import get_model, get_ollama_endpoint
from agents.coding.memory import MemoryLog

AIOS_ROOT = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os")))

REVIEWER_SYSTEM_PROMPT = textwrap.dedent("""\
    You are a conservative code reviewer embedded in the AI-OS system.
    Your role is to review proposed code changes and decide: APPROVE or REJECT.

    Review criteria:
    - Does the change match the stated task? (reject scope creep)
    - Does the change preserve existing behavior for unchanged functionality?
    - Is the change safe? (no destructive operations, no external side effects)
    - Is the change minimal? (reject unnecessary additions)
    - Are there any obvious bugs or security issues?

    Output format (strictly):
    DECISION: APPROVE
    REASONS:
    - <reason 1>
    - <reason 2>

    or

    DECISION: REJECT
    REASONS:
    - <reason 1>
    - <reason 2>

    Be specific. Do not add explanations outside this format.
""")


def _ollama_generate(prompt: str, model: str, endpoint: str, timeout: int = 90) -> str:
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 512},
    }).encode()

    req = urllib.request.Request(
        f"{endpoint}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            return data.get("response", "").strip()
    except urllib.error.URLError as e:
        from agents.coding.router import get_cloud_fallback_config, cloud_generate
        fb = get_cloud_fallback_config()
        if fb and fb["trigger"] == "ollama_unreachable":
            return cloud_generate(prompt, fb["model"])
        raise RuntimeError(f"Ollama unreachable at {endpoint}: {e}") from e


def _parse_decision(response: str) -> tuple[str, list[str]]:
    """
    Parse reviewer response into (decision, reasons).
    decision: "APPROVE" | "REJECT" | "UNKNOWN"
    """
    lines = response.strip().splitlines()
    decision = "UNKNOWN"
    reasons = []
    in_reasons = False

    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith("DECISION:"):
            d = stripped.split(":", 1)[1].strip().upper()
            if "APPROVE" in d:
                decision = "APPROVE"
            elif "REJECT" in d:
                decision = "REJECT"
        elif stripped.upper().startswith("REASONS:"):
            in_reasons = True
        elif in_reasons and stripped.startswith("-"):
            reasons.append(stripped[1:].strip())

    # Fallback: if response contains APPROVE/REJECT anywhere
    if decision == "UNKNOWN":
        upper = response.upper()
        if "APPROVE" in upper:
            decision = "APPROVE"
        elif "REJECT" in upper:
            decision = "REJECT"

    return decision, reasons


class ReviewResult:
    def __init__(self, decision: str, reasons: list[str], model: str, raw: str):
        self.decision = decision   # "APPROVE" | "REJECT" | "UNKNOWN"
        self.reasons = reasons
        self.model = model
        self.raw = raw
        self.approved = decision == "APPROVE"

    def __str__(self):
        return f"DECISION: {self.decision}\nREASONS:\n" + "\n".join(f"  - {r}" for r in self.reasons)

    def to_dict(self):
        return {
            "decision": self.decision,
            "approved": self.approved,
            "reasons": self.reasons,
            "model": self.model,
        }


class Reviewer:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.memory = MemoryLog(task_id)

    def review(
        self,
        task_description: str,
        plan: str,
        patches: dict = None,
    ) -> ReviewResult:
        """
        Review a plan (and optional patches) for a given task.
        Returns ReviewResult with approve/reject and reasons.
        """
        endpoint = get_ollama_endpoint()
        model = get_model("review")

        patch_block = ""
        if patches:
            parts = []
            for path, content in patches.items():
                parts.append(f"=== {path} ===\n{content[:2000]}")  # cap at 2000 chars
            patch_block = "\n\n".join(parts)

        changes_section = ("PROPOSED CHANGES:\n" + patch_block) if patch_block else ""
        prompt = textwrap.dedent(f"""\
            {REVIEWER_SYSTEM_PROMPT}

            ORIGINAL TASK:
            {task_description}

            PROPOSED PLAN:
            {plan}

            {changes_section}

            Review the above and output your DECISION and REASONS.
        """)

        raw = _ollama_generate(prompt, model, endpoint)
        decision, reasons = _parse_decision(raw)

        result = ReviewResult(decision, reasons, model, raw)
        self.memory.log_review(decision, reasons)

        return result
