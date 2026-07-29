"""
Tool implementations exposed to the LLM agent.

Every tool operates relative to a `repo_root` directory that is fixed when the
ToolBox is constructed. All paths are resolved and checked to make sure they
stay inside the repo, so the agent can never read or write files outside the
target project.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

IGNORE_DIRS = {".git", "node_modules", "dist", "build", "__pycache__", ".idea", ".vscode"}

# Commands the agent is allowed to run during implementation / verification.
# Kept intentionally narrow -- this is not a general purpose shell.
ALLOWED_COMMANDS = {
    "npm install",
    "npm ci",
    "npm run build",
    "node -v",
    "git status",
    "git diff",
    "git diff --stat",
    "git add -A",
    "git log --oneline -5",
}
ALLOWED_PREFIXES = ("node -c ", "node --check ", "npx ", "npm test")


@dataclass
class ToolResult:
    ok: bool
    output: str


class ToolBox:
    def __init__(self, repo_root: str, allow_write: bool = False, allow_shell: bool = False):
        self.repo_root = Path(repo_root).resolve()
        self.allow_write = allow_write
        self.allow_shell = allow_shell
        self.files_written: list[str] = []

    # ---------------------------------------------------------------- utils
    def _resolve(self, rel_path: str) -> Path:
        p = (self.repo_root / rel_path).resolve()
        if self.repo_root not in p.parents and p != self.repo_root:
            raise ValueError(f"Path '{rel_path}' escapes the repository root.")
        return p

    # ---------------------------------------------------------------- tools
    def list_directory(self, path: str = ".", max_depth: int = 4) -> ToolResult:
        root = self._resolve(path)
        if not root.exists():
            return ToolResult(False, f"Path does not exist: {path}")
        lines = []
        base_depth = len(root.parts)
        for p in sorted(root.rglob("*")):
            if any(part in IGNORE_DIRS for part in p.parts):
                continue
            depth = len(p.parts) - base_depth
            if depth > max_depth:
                continue
            rel = p.relative_to(self.repo_root)
            marker = "/" if p.is_dir() else ""
            lines.append(f"{'  ' * (depth - 1)}{rel.name}{marker}")
        return ToolResult(True, "\n".join(lines) if lines else "(empty directory)")

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> ToolResult:
        f = self._resolve(path)
        if not f.exists() or not f.is_file():
            return ToolResult(False, f"File does not exist: {path}")
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            return ToolResult(False, f"Could not read {path}: {e}")
        lines = text.splitlines()
        end_line = end_line or len(lines)
        chunk = lines[start_line - 1:end_line]
        numbered = "\n".join(f"{i + start_line:>5}\t{line}" for i, line in enumerate(chunk))
        return ToolResult(True, numbered or "(empty file)")

    def search_code(self, pattern: str, path: str = ".", regex: bool = True) -> ToolResult:
        root = self._resolve(path)
        try:
            compiled = re.compile(pattern) if regex else re.compile(re.escape(pattern))
        except re.error as e:
            return ToolResult(False, f"Invalid regex: {e}")
        matches = []
        files_iter = [root] if root.is_file() else root.rglob("*")
        for p in files_iter:
            if not p.is_file():
                continue
            if any(part in IGNORE_DIRS for part in p.parts):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:  # noqa: BLE001
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                if compiled.search(line):
                    rel = p.relative_to(self.repo_root)
                    matches.append(f"{rel}:{i}: {line.strip()}")
            if len(matches) > 300:
                break
        if not matches:
            return ToolResult(True, "No matches found.")
        return ToolResult(True, "\n".join(matches[:300]))

    def write_file(self, path: str, content: str) -> ToolResult:
        if not self.allow_write:
            return ToolResult(False, "write_file is disabled in this phase.")
        f = self._resolve(path)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
        self.files_written.append(str(f.relative_to(self.repo_root)))
        return ToolResult(True, f"Wrote {len(content)} bytes to {path}")

    def edit_file(self, path: str, old_str: str, new_str: str) -> ToolResult:
        """Precise find-and-replace edit. old_str must be unique in the file."""
        if not self.allow_write:
            return ToolResult(False, "edit_file is disabled in this phase.")
        f = self._resolve(path)
        if not f.exists():
            return ToolResult(False, f"File does not exist: {path}")
        text = f.read_text(encoding="utf-8")
        count = text.count(old_str)
        if count == 0:
            return ToolResult(False, "old_str not found in file. Re-read the file to get exact text.")
        if count > 1:
            return ToolResult(False, f"old_str is not unique ({count} occurrences). Provide more context.")
        f.write_text(text.replace(old_str, new_str, 1), encoding="utf-8")
        self.files_written.append(str(f.relative_to(self.repo_root)))
        return ToolResult(True, f"Edited {path}")

    def run_command(self, command: str) -> ToolResult:
        if not self.allow_shell:
            return ToolResult(False, "run_command is disabled in this phase.")
        normalized = command.strip()
        if normalized not in ALLOWED_COMMANDS and not normalized.startswith(ALLOWED_PREFIXES):
            return ToolResult(
                False,
                f"Command not permitted: '{command}'. "
                f"Allowed: {sorted(ALLOWED_COMMANDS)} or prefixes {ALLOWED_PREFIXES}",
            )
        try:
            proc = subprocess.run(
                normalized, shell=True, cwd=self.repo_root, capture_output=True,
                text=True, timeout=180,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            return ToolResult(proc.returncode == 0, out[-4000:] or "(no output)")
        except subprocess.TimeoutExpired:
            return ToolResult(False, "Command timed out after 180s.")


# --------------------------------------------------------------------------
# Tool schemas (Anthropic "tools" format) grouped by which phase may use them
# --------------------------------------------------------------------------

READ_ONLY_TOOLS = [
    {
        "name": "list_directory",
        "description": "List files and subdirectories under a path in the repository, "
                        "skipping node_modules/.git/build folders.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path, default '.' (repo root)."},
                "max_depth": {"type": "integer", "description": "Max recursion depth, default 4."},
            },
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file from the repository, with line numbers. "
                        "Optionally restrict to a line range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_code",
        "description": "Regex search across files under a path (like grep -rn). "
                        "Use to find route definitions, model schemas, imports, usages, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "Default '.'"},
            },
            "required": ["pattern"],
        },
    },
]

WRITE_TOOLS = [
    {
        "name": "write_file",
        "description": "Create a new file or fully overwrite an existing file with the given content. "
                        "Use for new files; prefer edit_file for small changes to existing files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace an exact, unique snippet of text in an existing file with new text. "
                        "old_str must match the file content exactly (including whitespace) and appear "
                        "exactly once. Always read_file first to copy the exact text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_str": {"type": "string"},
                "new_str": {"type": "string"},
            },
            "required": ["path", "old_str", "new_str"],
        },
    },
]

SHELL_TOOL = [
    {
        "name": "run_command",
        "description": "Run a whitelisted shell command in the repo root for verification purposes "
                        "(e.g. 'node -c app/controllers/note.controller.js' to syntax-check a file, "
                        "'npm install' to install deps, 'git diff --stat' to review changes).",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
]


def dispatch(toolbox: ToolBox, name: str, tool_input: dict) -> ToolResult:
    fn = {
        "list_directory": toolbox.list_directory,
        "read_file": toolbox.read_file,
        "search_code": toolbox.search_code,
        "write_file": toolbox.write_file,
        "edit_file": toolbox.edit_file,
        "run_command": toolbox.run_command,
    }.get(name)
    if fn is None:
        return ToolResult(False, f"Unknown tool: {name}")
    try:
        return fn(**tool_input)
    except TypeError as e:
        return ToolResult(False, f"Bad arguments for {name}: {e}")
    except Exception as e:  # noqa: BLE001
        return ToolResult(False, f"Tool {name} raised an error: {e}")
