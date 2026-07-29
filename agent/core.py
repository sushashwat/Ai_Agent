from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import prompts
from .llm_client import LLMClient
from .tools import READ_ONLY_TOOLS, WRITE_TOOLS, SHELL_TOOL, ToolBox


@dataclass
class RunResult:
    exploration_report: str = ""
    plan: dict = field(default_factory=dict)
    implementation_notes: str = ""
    diff: str = ""
    summary: str = ""
    files_touched: list[str] = field(default_factory=list)


class CodingAgent:
    """Explores a repo, plans a feature, implements it, and summarizes the diff."""

    def __init__(self, repo_root: str, model: str | None = None, verbose: bool = True):
        self.repo_root = Path(repo_root).resolve()
        self.llm = LLMClient(model=model, verbose=verbose)
        self.verbose = verbose

    def _log(self, msg: str):
        if self.verbose:
            print(msg, flush=True)

    # ---------------------------------------------------------------- steps
    def explore(self) -> str:
        self._log("\n=== PHASE 1: Repository Exploration ===")
        toolbox = ToolBox(str(self.repo_root), allow_write=False, allow_shell=False)
        report = self.llm.run_tool_loop(
            system_prompt=prompts.EXPLORE_SYSTEM,
            user_prompt=(
                "Explore this repository and produce the report described in your "
                "instructions. Start with `list_directory('.')`."
            ),
            tools=READ_ONLY_TOOLS,
            toolbox=toolbox,
            max_turns=12,
        )
        return report

    def plan(self, request: str, exploration_report: str) -> dict:
        self._log("\n=== PHASE 2: Planning ===")
        user_prompt = (
            f"Product request from the user:\n\"\"\"\n{request}\n\"\"\"\n\n"
            f"Exploration report of the codebase:\n\"\"\"\n{exploration_report}\n\"\"\"\n\n"
            "Produce the JSON implementation plan now."
        )
        raw = self.llm.complete(prompts.PLAN_SYSTEM, user_prompt, max_tokens=2048)
        plan = _parse_json(raw)
        self._log(f"  Chosen approach: {plan.get('approach_name', '?')}")
        for step in plan.get("steps", []):
            self._log(f"    - [{step.get('action')}] {step.get('path')}: {step.get('description')}")
        return plan

    def implement(self, request: str, plan: dict) -> tuple[str, ToolBox]:
        self._log("\n=== PHASE 3: Implementation ===")
        toolbox = ToolBox(str(self.repo_root), allow_write=True, allow_shell=True)
        plan_text = json.dumps(plan, indent=2)
        user_prompt = (
            f"Original request:\n\"\"\"\n{request}\n\"\"\"\n\n"
            f"Approved plan:\n\"\"\"\n{plan_text}\n\"\"\"\n\n"
            "Implement every step of this plan now, using your tools. Read files before "
            "editing them. Verify your work with run_command at the end."
        )
        notes = self.llm.run_tool_loop(
            system_prompt=prompts.IMPLEMENT_SYSTEM,
            user_prompt=user_prompt,
            tools=READ_ONLY_TOOLS + WRITE_TOOLS + SHELL_TOOL,
            toolbox=toolbox,
            max_turns=30,
        )
        return notes, toolbox

    def diff(self) -> str:
        try:
            proc = subprocess.run(
                ["git", "diff", "--stat", "HEAD"], cwd=self.repo_root,
                capture_output=True, text=True, timeout=30,
            )
            stat = proc.stdout
            proc2 = subprocess.run(
                ["git", "diff", "HEAD"], cwd=self.repo_root,
                capture_output=True, text=True, timeout=30,
            )
            full = proc2.stdout
            # also include untracked new files
            proc3 = subprocess.run(
                ["git", "status", "--porcelain"], cwd=self.repo_root,
                capture_output=True, text=True, timeout=30,
            )
            untracked = [l[3:] for l in proc3.stdout.splitlines() if l.startswith("??")]
            extra = ""
            for f in untracked:
                p = self.repo_root / f
                if p.is_file():
                    try:
                        extra += f"\n--- new file: {f} ---\n" + p.read_text(errors="replace")
                    except Exception:
                        pass
            return f"{stat}\n{full}{extra}"
        except Exception as e:  # noqa: BLE001
            return f"(could not compute git diff: {e})"

    def summarize(self, request: str, plan: dict, diff_text: str) -> str:
        self._log("\n=== PHASE 4: Summary ===")
        user_prompt = (
            f"Original request:\n\"\"\"\n{request}\n\"\"\"\n\n"
            f"Plan:\n\"\"\"\n{json.dumps(plan, indent=2)}\n\"\"\"\n\n"
            f"git diff of the actual changes:\n\"\"\"\n{diff_text[:15000]}\n\"\"\"\n\n"
            "Write the PR summary now."
        )
        return self.llm.complete(prompts.SUMMARY_SYSTEM, user_prompt, max_tokens=2048)

    # ---------------------------------------------------------------- run
    def run(self, request: str) -> RunResult:
        result = RunResult()
        result.exploration_report = self.explore()
        result.plan = self.plan(request, result.exploration_report)
        result.implementation_notes, toolbox = self.implement(request, result.plan)
        result.files_touched = toolbox.files_written
        result.diff = self.diff()
        result.summary = self.summarize(request, result.plan, result.diff)
        return result


def _parse_json(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise
