"""
agents/coding/safe_exec.py — Command safety policy for the coding subsystem.

Commands are evaluated before execution.
Safe commands are allowed directly.
Blocked commands raise SafeExecError.
Commands requiring approval raise ApprovalRequiredError.
"""
import re
import shlex
import subprocess
import pathlib
import os

AIOS_ROOT = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os")))


class SafeExecError(Exception):
    """Raised when a command is blocked by policy."""


class ApprovalRequiredError(Exception):
    """Raised when a command requires explicit human approval."""


# ── Allow list — safe repo-local inspection commands ─────────────────────────

SAFE_PREFIXES = (
    "pwd",
    "ls",
    "cat ",
    "grep ",
    "find ",
    "wc ",
    "head ",
    "tail ",
    "diff ",
    "git status",
    "git diff",
    "git log",
    "git show",
    "git branch",
    "git remote",
    "python3 -m py_compile",
    "python3 -c",
    "python3 -m mypy",
    "python3 -m flake8",
    "python3 -m pylint",
    "python3 -m pytest",
    "pylint ",
    "flake8 ",
    "mypy ",
    "black --check",
    "black --diff",
    "isort --check",
    "isort --diff",
    "echo ",
    "true",
    "false",
)

SAFE_EXACT = {
    "pwd", "ls", "git status", "git diff", "true", "false",
}

# ── Block list — dangerous or destructive commands ───────────────────────────

BLOCKED_PATTERNS = [
    # Destructive filesystem
    r"\brm\b",
    r"\brmdir\b",
    r"\bshred\b",
    r"\btruncate\b",
    r"\bdd\b",
    r"\bmkfs\b",
    r"\bformat\b",
    # System power
    r"\breboot\b",
    r"\bshutdown\b",
    r"\bpoweroff\b",
    r"\bhalt\b",
    r"\binit\b\s+0",
    r"\binit\b\s+6",
    # Package management (unapproved)
    r"\bapt\b",
    r"\bapt-get\b",
    r"\byum\b",
    r"\bdnf\b",
    r"\bpip\b\s+install",
    r"\bpip3\b\s+install",
    r"\bnpm\b\s+install",
    # Privilege escalation
    r"\bsudo\b",
    r"\bsu\b\s",
    r"\bchroot\b",
    # Permission changes on broad paths
    r"\bchmod\b\s+.*-R",
    r"\bchown\b\s+.*-R",
    # Docker destructive
    r"\bdocker\b\s+system\s+prune",
    r"\bdocker\b\s+volume\s+rm",
    r"\bdocker\b\s+rm\b",
    r"\bdocker\b\s+rmi\b",
    # Writes outside repo (path traversal)
    r">\s*/(?!home/jdl/ai-os)",
    r">\s*\.\./\.\.",
    r"tee\s+/",
    # Network exfiltration / download
    r"\bcurl\b.*-o\s+/",
    r"\bwget\b.*-O\s+/",
    # Database drops
    r"\bDROP\s+TABLE\b",
    r"\bDROP\s+DATABASE\b",
    r"\bTRUNCATE\b\s+TABLE",
    # Git destructive
    r"\bgit\s+push\b.*--force",
    r"\bgit\s+reset\b.*--hard",
    r"\bgit\s+clean\b.*-[fd]",
    r"\bgit\s+branch\b.*-[Dd]",
]

_BLOCKED_RE = re.compile("|".join(BLOCKED_PATTERNS), re.IGNORECASE)

# ── Approval required ─────────────────────────────────────────────────────────

APPROVAL_PATTERNS = [
    r"\bgit\s+commit\b",
    r"\bgit\s+push\b",
    r"\bgit\s+merge\b",
    r"\bgit\s+rebase\b",
    r"\bwrite_file\b",
    r"\bpatch\b",
    r"\bchmod\b",
    r"\bchown\b",
    r"\bsystemctl\b",
    r"\bservice\b",
]

_APPROVAL_RE = re.compile("|".join(APPROVAL_PATTERNS), re.IGNORECASE)


def _is_outside_repo(cmd: str) -> bool:
    """Detect writes targeting paths outside the AI-OS repo."""
    outside = re.search(r">\s*/(?!home/jdl/ai-os)", cmd)
    return bool(outside)


def classify_command(cmd: str) -> str:
    """
    Returns: "safe" | "blocked" | "approval_required"
    """
    cmd_stripped = cmd.strip()

    # Exact safe match
    if cmd_stripped in SAFE_EXACT:
        return "safe"

    # Blocked first (takes priority over approval)
    if _BLOCKED_RE.search(cmd_stripped):
        return "blocked"

    # Approval required
    if _APPROVAL_RE.search(cmd_stripped):
        return "approval_required"

    # Safe prefix
    for prefix in SAFE_PREFIXES:
        if cmd_stripped.startswith(prefix):
            return "safe"

    # Unknown → require approval (fail safe)
    return "approval_required"


def is_safe_command(cmd: str) -> bool:
    """Return True if the command is safe to run without manual approval."""
    return classify_command(cmd) == "safe"


def run_safe(
    cmd: str,
    cwd: str = None,
    timeout: int = 30,
    approved: bool = False,
) -> tuple[int, str, str]:
    """
    Run a shell command after safety check.
    Returns (returncode, stdout, stderr).
    Raises SafeExecError if blocked.
    Raises ApprovalRequiredError if approval needed and not given.
    """
    classification = classify_command(cmd)

    if classification == "blocked":
        raise SafeExecError(f"Command blocked by policy: {cmd!r}")

    if classification == "approval_required" and not approved:
        raise ApprovalRequiredError(
            f"Command requires explicit approval: {cmd!r}"
        )

    result = subprocess.run(
        cmd,
        shell=True,
        cwd=cwd or str(AIOS_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.returncode, result.stdout, result.stderr


if __name__ == "__main__":
    import sys
    cmd = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "ls"
    c = classify_command(cmd)
    print(f"classify({cmd!r}) = {c}")
