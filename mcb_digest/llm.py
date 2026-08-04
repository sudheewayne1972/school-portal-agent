"""Thin Azure OpenAI wrapper used by the agent.

Reads config from environment (populated by `.env` via `config._load_env_file`):
  AZURE_OPENAI_ENDPOINT
  AZURE_OPENAI_KEY
  AZURE_OPENAI_DEPLOYMENT
  AZURE_OPENAI_API_VERSION (default 2024-10-21)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

from openai import AzureOpenAI


@dataclass
class LLMConfig:
    endpoint: str
    api_key: str
    deployment: str
    api_version: str = "2024-10-21"


def load_llm_config() -> LLMConfig:
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
    key = os.environ.get("AZURE_OPENAI_KEY", "").strip()
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "").strip()
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21").strip()
    missing = [k for k, v in {
        "AZURE_OPENAI_ENDPOINT": endpoint,
        "AZURE_OPENAI_KEY": key,
        "AZURE_OPENAI_DEPLOYMENT": deployment,
    }.items() if not v]
    if missing:
        raise RuntimeError(f"Missing env vars: {', '.join(missing)}")
    return LLMConfig(endpoint=endpoint, api_key=key, deployment=deployment, api_version=api_version)


class LLM:
    def __init__(self, cfg: Optional[LLMConfig] = None) -> None:
        self.cfg = cfg or load_llm_config()
        self._client = AzureOpenAI(
            azure_endpoint=self.cfg.endpoint,
            api_key=self.cfg.api_key,
            api_version=self.cfg.api_version,
        )

    def chat(
        self,
        messages: List[dict],
        *,
        max_output_tokens: int = 800,
        temperature: float = 0.2,
    ) -> str:
        resp = self._client.chat.completions.create(
            model=self.cfg.deployment,
            messages=messages,
            max_completion_tokens=max_output_tokens,
            temperature=temperature,
        )
        return (resp.choices[0].message.content or "").strip()
