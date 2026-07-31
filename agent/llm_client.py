"""
Wrapper around the Google Gemini API (new google-genai SDK) implementing the
same tool-use loop interface as the original Anthropic client, so core.py /
main.py don't need any changes. Includes automatic retry on rate limits for
both tool-calling and single-shot completions.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

from google import genai
from google.genai import types

from .tools import ToolBox, dispatch

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")


@dataclass
class AgentTurn:
    text: str
    tool_calls: list[dict]


class LLMClient:
    def __init__(self, model: str | None = None, verbose: bool = True):
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self.model_name = model or DEFAULT_MODEL
        self.verbose = verbose

    def _log(self, msg: str):
        if self.verbose:
            print(msg, flush=True)

    @staticmethod
    def _to_gemini_tools(tools: list[dict]):
        if not tools:
            return None
        declarations = [
            types.FunctionDeclaration(
                name=t["name"],
                description=t["description"],
                parameters=t["input_schema"],
            )
            for t in tools
        ]
        return [types.Tool(function_declarations=declarations)]

    def _wait_for_rate_limit(self, error: Exception, attempt: int, max_attempts: int) -> None:
        match = re.search(r"retryDelay['\"]?:\s*['\"]?(\d+)", str(error))
        wait = int(match.group(1)) + 2 if match else 15
        self._log(f"  [rate-limit] Waiting {wait}s before retry ({attempt + 1}/{max_attempts})...")
        time.sleep(wait)

    def _send_with_retry(self, chat, message, max_attempts: int = 5):
        for attempt in range(max_attempts):
            try:
                return chat.send_message(message)
            except Exception as e:  # noqa: BLE001
                if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e):
                    self._wait_for_rate_limit(e, attempt, max_attempts)
                else:
                    raise
        raise RuntimeError("Failed after retries due to rate limiting.")

    def run_tool_loop(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[dict],
        toolbox: ToolBox,
        max_turns: int = 20,
        max_tokens: int = 4096,
    ) -> str:
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=self._to_gemini_tools(tools),
        )
        chat = self.client.chats.create(model=self.model_name, config=config)
        message = user_prompt
        final_text = ""

        for turn in range(1, max_turns + 1):
            response = self._send_with_retry(chat, message)
            calls = response.function_calls or []
            text = response.text or ""

            if text.strip():
                final_text = text
                self._log(f"  [agent] {text.strip()[:400]}")

            if not calls:
                return final_text

            parts = []
            for call in calls:
                name = call.name
                args = dict(call.args) if call.args else {}
                self._log(f"  [tool] {name}({_short(args)})")
                result = dispatch(toolbox, name, args)
                preview = result.output[:200].replace("\n", " ")
                self._log(f"    -> {'ok' if result.ok else 'ERROR'}: {preview}")
                parts.append(
                    types.Part.from_function_response(
                        name=name, response={"result": result.output}
                    )
                )
            message = parts

        self._log("  [warn] Reached max tool-loop turns; stopping.")
        return final_text

    def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 4096) -> str:
        config = types.GenerateContentConfig(system_instruction=system_prompt)
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                response = self.client.models.generate_content(
                    model=self.model_name, contents=user_prompt, config=config
                )
                return response.text
            except Exception as e:  # noqa: BLE001
                if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e):
                    self._wait_for_rate_limit(e, attempt, max_attempts)
                else:
                    raise
        raise RuntimeError("Failed after retries due to rate limiting.")


def _short(d: dict) -> str:
    s = str(d)
    return s if len(s) < 120 else s[:117] + "..."