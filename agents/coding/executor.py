"""
agents/coding/executor.py — Apply approved changes only.

Never bypasses review. Logs all actions. Refuses unsafe or unapproved operations.
In dry-run mode, shows what would happen without making changes.
"""
import os
import pathlib
import subprocess
import textwrap
import datetime

from agents.coding.memory import MemoryLog
from agents.coding.safe_exec import run_safe, SafeExecError, ApprovalRequiredError, classify_command
from agents.coding.reviewer import ReviewResult

AIOS_ROOT = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os")))


class ExecutionResult:
    def __init__(
        self,
        task_id: str,
        actions: list[str],
        outcome: str,
        diff: str = "",
        skipped: bool = False,
        skip_reason: str = "",
    ):
        self.task_id = task_id
        self.actions = actions
        self.outcome = outcome
        self.diff = diff
        self.skipped = skipped
        self.skip_reason = skip_reason

    def __str__(self):
        """Return a human-readable summary of the execution result."""
        if self.skipped:
            return f"SKIPPED: {self.skip_reason}"
        return f"OUTCOME: {self.outcome}\nACTIONS:\n" + "\n".join(f"  {a}" for a in self.actions)


class Executor:
    def __init__(self, task_id: str, dry_run: bool = False):
        self.task_id = task_id
        self.dry_run = dry_run
        self.memory = MemoryLog(task_id)

    def execute(
        self,
        task_description: str,
        review_result: ReviewResult,
        patches: dict = None,
        post_validate_cmd: str = None,
    ) -> ExecutionResult:
        """
        Apply changes if and only if review_result.approved is True.

        patches: dict of {relative_path: new_file_content}
        post_validate_cmd: optional safe command to run after changes (e.g. syntax check)
        """
        if not review_result.approved:
            reason = f"Review rejected: {'; '.join(review_result.reasons)}"
            self.memory.log_skip(reason)
            return ExecutionResult(
                task_id=self.task_id,
                actions=[],
                outcome="skipped",
                skipped=True,
                skip_reason=reason,
            )

        actions = []
        diffs = []

        if self.dry_run:
            if patches:
                for path, content in patches.items():
                    actions.append(f"[DRY-RUN] would write: {path}")
            if post_validate_cmd:
                actions.append(f"[DRY-RUN] would run: {post_validate_cmd}")
            self.memory.log_execution(actions)
            return ExecutionResult(
                task_id=self.task_id,
                actions=actions,
                outcome="dry_run",
            )

        # Apply patches
        if patches:
            for rel_path, new_content in patches.items():
                abs_path = (
                    AIOS_ROOT / rel_path
                    if not pathlib.Path(rel_path).is_absolute()
                    else pathlib.Path(rel_path)
                )

                # Safety: must be inside AIOS_ROOT
                try:
                    abs_path.resolve().relative_to(AIOS_ROOT.resolve())
                except ValueError:
                    self.memory.log_failure(
                        "executor",
                        f"Path outside repo: {rel_path}",
                    )
                    raise SafeExecError(f"Refused: path outside repo: {rel_path}")

                # Capture current content for diff
                old_content = abs_path.read_text(errors="replace") if abs_path.exists() else ""

                abs_path.parent.mkdir(parents=True, exist_ok=True)
                abs_path.write_text(new_content)
                actions.append(f"written: {rel_path}")

                # Generate simple line diff summary
                old_lines = old_content.splitlines()
                new_lines = new_content.splitlines()
                added = sum(1 for l in new_lines if l not in old_lines)
                removed = sum(1 for l in old_lines if l not in new_lines)
                diffs.append(f"{rel_path}: +{added}/-{removed} lines")

        # Run post-validation if provided
        if post_validate_cmd:
            cls = classify_command(post_validate_cmd)
            if cls == "blocked":
                self.memory.log_failure("executor", f"Post-validation command blocked: {post_validate_cmd}")
                raise SafeExecError(f"Post-validation command blocked: {post_validate_cmd}")

            approved_for_run = cls == "safe"
            try:
                rc, stdout, stderr = run_safe(
                    post_validate_cmd,
                    cwd=str(AIOS_ROOT),
                    timeout=60,
                    approved=approved_for_run,
                )
                if rc == 0:
                    actions.append(f"validated: {post_validate_cmd} -> OK")
                else:
                    actions.append(f"validation warning: {post_validate_cmd} -> rc={rc}")
                    if stderr:
                        actions.append(f"  stderr: {stderr[:200]}")
            except (SafeExecError, ApprovalRequiredError) as e:
                actions.append(f"validation skipped: {e}")

        diff_summary = "\n".join(diffs)
        self.memory.log_execution(actions, "completed", diff=diff_summary)

        return ExecutionResult(
            task_id=self.task_id,
            actions=actions,
            outcome="completed",
            diff=diff_summary,
        )

    def run_safe_cmd(self, cmd: str) -> tuple[int, str, str]:
        """
        Run a safe command in repo context.
        Raises SafeExecError if not allowed.
        """
        cls = classify_command(cmd)
        if cls == "blocked":
            raise SafeExecError(f"Blocked: {cmd}")
        if cls == "approval_required":
            raise ApprovalRequiredError(f"Approval required: {cmd}")
        return run_safe(cmd, cwd=str(AIOS_ROOT), approved=False)
