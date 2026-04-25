"""
agents/coding/engineer.py — Generate conservative implementation plans.

Given a task description and target file(s), produces a structured plan
for a code change. Uses the coding/plan model from router config.
The plan is text-based and safe to inspect before any execution.
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

ENGINEER_SYSTEM_PROMPT = textwrap.dedent("""\
    You are a conservative software engineer assistant embedded in the AI-OS system.
    Your role is to produce clear, minimal, safe implementation plans for code changes.

    Rules:
    - Be conservative. Prefer minimal changes that achieve the goal.
    - Do not change behavior unless explicitly asked.
    - Produce a numbered list of specific, actionable steps.
    - For each step, state: what file, what line/function, and what change.
    - If a change is risky or ambiguous, flag it explicitly.
    - Do not add features beyond what was requested.
    - Format: structured plain text, no markdown headers needed.
""")


def _ollama_generate(prompt: str, model: str, endpoint: str, timeout: int = 120) -> str:
    """Call Ollama /api/generate and return the response text."""
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 1024},
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


def _read_file_snippet(path: pathlib.Path, max_lines: int = 100) -> str:
    """Read up to max_lines of a file for context."""
    if not path.exists():
        return f"[file not found: {path}]"
    lines = path.read_text(errors="replace").splitlines()
    if len(lines) <= max_lines:
        return "\n".join(f"{i+1}: {l}" for i, l in enumerate(lines))
    # Show first 60 and last 40
    head = "\n".join(f"{i+1}: {l}" for i, l in enumerate(lines[:60]))
    tail = "\n".join(f"{i+1}: {l}" for i, l in enumerate(lines[-40:], len(lines) - 40))
    return f"{head}\n... ({len(lines) - 100} lines omitted) ...\n{tail}"


def _strip_markdown_fences(content: str) -> str:
    """Remove markdown code fences if the LLM wrapped file content in them.

    Handles both ```python\\n...\\n``` and plain ```\\n...\\n``` variants.
    If no fence is present the content is returned unchanged.
    """
    import re
    m = re.match(r"^```[a-zA-Z0-9_-]*\s*\n(.*?)\n```\s*$", content, re.DOTALL)
    return m.group(1) if m else content


class Engineer:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.memory = MemoryLog(task_id)

    def plan(
        self,
        task_description: str,
        target_files: list[str],
        context: dict = None,
    ) -> str:
        """
        Generate a conservative implementation plan.

        Returns the plan as a string.
        Also logs the plan to memory.
        """
        endpoint = get_ollama_endpoint()
        model = get_model("plan")

        self.memory.log_task(task_description, context=context)

        # Build file context
        file_contexts = []
        for f in target_files:
            p = AIOS_ROOT / f if not pathlib.Path(f).is_absolute() else pathlib.Path(f)
            snippet = _read_file_snippet(p)
            file_contexts.append(f"=== {f} ===\n{snippet}")

        file_block = "\n\n".join(file_contexts) if file_contexts else "(no files specified)"

        prompt = textwrap.dedent(f"""\
            {ENGINEER_SYSTEM_PROMPT}

            TASK:
            {task_description}

            TARGET FILES:
            {file_block}

            Produce a numbered implementation plan. Be conservative and specific.
        """)

        plan = _ollama_generate(prompt, model, endpoint)
        self.memory.log_plan(plan, model)
        return plan

    def plan_with_patch(
        self,
        task_description: str,
        target_files: list[str],
        context: dict = None,
    ) -> dict:
        """
        Generate plan AND produce a proposed code patch in unified diff format.
        Returns {"plan": str, "patch": str, "model": str}.
        """
        endpoint = get_ollama_endpoint()
        model = get_model("coding")

        self.memory.log_task(task_description, context=context)

        file_contexts = []
        for f in target_files:
            p = AIOS_ROOT / f if not pathlib.Path(f).is_absolute() else pathlib.Path(f)
            snippet = _read_file_snippet(p)
            file_contexts.append(f"=== {f} ===\n{snippet}")

        file_block = "\n\n".join(file_contexts) if file_contexts else "(no files specified)"

        prompt = textwrap.dedent(f"""\
            {ENGINEER_SYSTEM_PROMPT}

            TASK:
            {task_description}

            TARGET FILES:
            {file_block}

            Produce:
            1. A numbered implementation plan (brief, specific).
            2. The complete modified file content for each changed file.

            For each changed file, output:
            === CHANGED FILE: <path> ===
            <full new file content>
            === END FILE ===
        """)

        response = _ollama_generate(prompt, model, endpoint)
        self.memory.log_plan(response, model)

        # Parse plan and patches from response
        plan_part = response
        patches = {}

        import re
        file_matches = re.findall(
            r"=== CHANGED FILE: (.+?) ===\n(.*?)=== END FILE ===",
            response,
            re.DOTALL,
        )
        for path_str, content in file_matches:
            patches[path_str.strip()] = _strip_markdown_fences(content.strip())
            plan_part = plan_part.replace(
                f"=== CHANGED FILE: {path_str} ===\n{content}=== END FILE ===", ""
            ).strip()

        return {"plan": plan_part, "patches": patches, "model": model}
