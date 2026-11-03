"""Model provider adapters."""

from .factory import default_summarizer
from .llm import OpenAICompatibleLLM

__all__ = ["OpenAICompatibleLLM", "default_summarizer"]
