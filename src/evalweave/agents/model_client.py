from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

from openai import OpenAI

from evalweave.core.config import AgentConfig


class AgentModelClient:
    """Single protocol adapter for all evaluation-model calls."""

    def __init__(self, config: AgentConfig):
        self.config = config
        self.client = OpenAI(
            api_key=config.api_key or "not-configured",
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            max_retries=0,
        )

    @property
    def api_mode(self) -> str:
        return self.config.api_mode

    @staticmethod
    def input_messages(
        system_prompt: str, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return [{"role": "system", "content": system_prompt}, *messages]

    def stream_text(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        *,
        json_mode: bool = False,
    ) -> Iterator[str]:
        input_messages = self.input_messages(system_prompt, messages)
        if self.api_mode == "responses":
            options: dict[str, Any] = {
                "model": self.config.model,
                "input": input_messages,
                "store": False,
                "stream": True,
            }
            if json_mode:
                options["text"] = {"format": {"type": "json_object"}}
            stream = self.client.responses.create(**options)
            for event in stream:
                if (
                    getattr(event, "type", "") == "response.output_text.delta"
                    and getattr(event, "delta", None)
                ):
                    yield str(event.delta)
            return

        options = {
            "model": self.config.model,
            "temperature": 0,
            "messages": input_messages,
            "stream": True,
        }
        if json_mode:
            options["response_format"] = {"type": "json_object"}
        stream = self.client.chat.completions.create(**options)
        for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield str(delta)

    def request_text(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        *,
        json_mode: bool = False,
        on_delta: Callable[[str], None] | None = None,
    ) -> str:
        if on_delta is not None:
            content = ""
            for delta in self.stream_text(system_prompt, messages, json_mode=json_mode):
                content += delta
                on_delta(delta)
            return content

        input_messages = self.input_messages(system_prompt, messages)
        if self.api_mode == "responses":
            options: dict[str, Any] = {
                "model": self.config.model,
                "input": input_messages,
                "store": False,
            }
            if json_mode:
                options["text"] = {"format": {"type": "json_object"}}
            response = self.client.responses.create(**options)
            return str(response.output_text or "")

        options = {
            "model": self.config.model,
            "temperature": 0,
            "messages": input_messages,
        }
        if json_mode:
            options["response_format"] = {"type": "json_object"}
        response = self.client.chat.completions.create(**options)
        return str(response.choices[0].message.content or "")

    def stream_tools(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Any:
        input_messages = self.input_messages(system_prompt, messages)
        if self.api_mode == "responses":
            return self.client.responses.create(
                model=self.config.model,
                input=input_messages,
                tools=[{"type": "function", **tool} for tool in tools],
                tool_choice="auto",
                store=False,
                stream=True,
            )
        return self.client.chat.completions.create(
            model=self.config.model,
            messages=input_messages,
            tools=[{"type": "function", "function": tool} for tool in tools],
            tool_choice="auto",
            temperature=0,
            stream=True,
        )
