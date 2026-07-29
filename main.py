#!/usr/bin/env python3
"""
AI Coding Agent CLI.

Usage:
    python main.py --repo /path/to/target/repo --request "Improve the application
    so users can better organise and search their notes."

Requires ANTHROPIC_API_KEY to be set in the environment (or a .env file, see
.env.example). Optionally set ANTHROPIC_MODEL to override the default model.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from agent import CodingAgent


def main():
    parser = argparse.ArgumentParser(description="AI coding agent for implementing product requests in a repo.")
    parser.add_argument("--repo", required=True, help="Path to the target repository (already cloned locally).")
    parser.add_argument("--request", required=True, help="The product requirement / feature request text.")
    parser.add_argument("--model", default=None, help="Override the Anthropic model id.")
    parser.add_argument("--out-dir", default=None, help="Where to write PLAN.md / CHANGES.md. Defaults to the repo root.")
    args = parser.parse_args()

    repo_path = Path(args.repo).resolve()
    if not repo_path.exists():
        print(f"Repo path does not exist: {repo_path}", file=sys.stderr)
        sys.exit(1)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. Export it or put it in a .env file.", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.out_dir).resolve() if args.out_dir else repo_path

    print(f"Target repo : {repo_path}")
    print(f"Request     : {args.request}\n")

    agent = CodingAgent(repo_root=str(repo_path), model=args.model, verbose=True)
    result = agent.run(args.request)

    (out_dir / "PLAN.md").write_text(
        "# Implementation Plan\n\n"
        f"## Exploration Report\n\n{result.exploration_report}\n\n"
        f"## Plan\n\n```json\n{_pretty(result.plan)}\n```\n",
        encoding="utf-8",
    )
    (out_dir / "CHANGES.md").write_text(
        f"# Change Summary\n\n{result.summary}\n\n"
        f"## Files touched\n\n" + "\n".join(f"- {f}" for f in result.files_touched) + "\n",
        encoding="utf-8",
    )

    print("\n=== DONE ===")
    print(f"Files touched: {result.files_touched}")
    print(f"Wrote {out_dir / 'PLAN.md'} and {out_dir / 'CHANGES.md'}")
    print("\n--- Summary ---\n")
    print(result.summary)


def _pretty(d: dict) -> str:
    import json
    return json.dumps(d, indent=2)


if __name__ == "__main__":
    main()
