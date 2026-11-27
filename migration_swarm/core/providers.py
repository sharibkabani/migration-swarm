"""LLM providers. Model ids are `<prefix>:<name>` (anthropic: / google_genai: / openai:), as in LangChain.

Each provider maps the role's `effort` onto its own reasoning control and reads its key from the env:
  claude   ANTHROPIC_API_KEY (or llm_backend: plan, the Claude Agent SDK on a Claude plan login)
  gemini   GOOGLE_API_KEY or GEMINI_API_KEY
  openai   OPENAI_API_KEY
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    prefix: str
    env_keys: tuple[str, ...]
    key_url: str
    note: str = ""

    def key(self) -> str | None:
        return next((os.environ[k] for k in self.env_keys if os.environ.get(k)), None)


PROVIDERS = {
    "claude": Provider("claude", "Claude (Anthropic)", "anthropic", ("ANTHROPIC_API_KEY",),
                       "https://platform.claude.com/settings/keys",
                       "Haiku 5.5 workers, Sonnet 5.5 for escalation. Can also run on your Claude plan login."),
    "gemini": Provider("gemini", "Gemini (Google)", "google_genai", ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
                       "https://aistudio.google.com/apikey",
                       "Gemini 3.8 Flash workers (free tier available), Gemini 4 Carbon for escalation."),
    "openai": Provider("openai", "OpenAI", "openai", ("OPENAI_API_KEY",),
                       "https://platform.openai.com/api-keys",
                       "GPT-6 Luna workers, GPT-6.2 Sol for escalation."),
}


def prefix_of(model: str) -> str:
    return model.split(":", 1)[0] if ":" in model else "anthropic"


def chat_model(model: str, effort: str, max_tokens: int, max_retries: int) -> BaseChatModel:
    prefix, name = prefix_of(model), model.split(":", 1)[-1]
    if prefix == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=name, max_tokens=max_tokens, max_retries=max_retries, streaming=True,
                             output_config={"effort": effort})
    if prefix == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=name, max_tokens=max_tokens, max_retries=max_retries, streaming=True,
                          stream_usage=True, reasoning_effort=effort)
    if prefix == "google_genai":
        from langchain_google_genai import ChatGoogleGenerativeAI
        key = PROVIDERS["gemini"].key()
        return ChatGoogleGenerativeAI(model=name, max_output_tokens=max_tokens, max_retries=max_retries,
                                      streaming=True, thinking_level=effort, **({"google_api_key": key} if key else {}))
    raise ValueError(f"unknown model provider prefix {prefix!r} in {model!r}")


def supports_cache_control(model: str) -> bool:
    return prefix_of(model) == "anthropic"
