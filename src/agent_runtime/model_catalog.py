"""The model catalog: per-model endpoint settings read from ``model.json``.

A deployment can offer more than one model, each with its own base URL and
API key, without restarting the runtime for every switch.  The catalog is
a plain JSON document::

    {
      "default": "main",
      "models": [
        {
          "id": "main",
          "label": "DeepSeek Chat",
          "base_url": "https://api.deepseek.com/v1",
          "api_key": "${DEEPSEEK_API_KEY}",
          "model_id": "deepseek-chat"
        }
      ]
    }

``id`` is the handle the web UI stores on a session; ``model_id`` is the
name the provider expects on the wire.  ``api_key`` accepts a literal
secret or an ``${ENV_VAR}`` / ``env:ENV_VAR`` reference, which is expanded
when the catalog is loaded.

The catalog is resolved once at the composition root and never exposed
verbatim: :meth:`ModelCatalog.public` drops the endpoint and the secret so
the browser only ever sees ids and labels.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CATALOG_ENV = "AGENT_RUNTIME_MODEL_CATALOG"
DEFAULT_CATALOG_NAME = "model.json"

# ``${NAME}`` and ``env:NAME`` both mean "read this from the environment".
_ENV_REF = re.compile(
    r"^(?:\$\{([A-Za-z_][A-Za-z0-9_]*)\}|env:([A-Za-z_][A-Za-z0-9_]*))$")


class ModelCatalogError(RuntimeError):
    """The catalog is missing, malformed, or names an unknown model."""


@dataclass(frozen=True)
class ModelEntry:
    """One selectable model and the endpoint that serves it."""

    id: str
    label: str
    base_url: str | None
    api_key: str | None
    model_id: str

    def public(self) -> dict[str, str]:
        """The browser-safe view: no endpoint, no secret."""
        return {"id": self.id, "label": self.label, "model_id": self.model_id}


@dataclass(frozen=True)
class ModelCatalog:
    """An ordered list of :class:`ModelEntry` values and its default."""

    entries: tuple[ModelEntry, ...]
    default: str

    def get(self, model_id: str | None) -> ModelEntry | None:
        if model_id is None:
            return None
        for entry in self.entries:
            if entry.id == model_id:
                return entry
        return None

    def require(self, model_id: str | None) -> ModelEntry:
        entry = self.get(model_id)
        if entry is None:
            raise ModelCatalogError(f"unknown model {model_id!r}")
        return entry

    @property
    def default_entry(self) -> ModelEntry:
        return self.require(self.default)

    def public(self) -> list[dict[str, str]]:
        return [entry.public() for entry in self.entries]


def repo_root() -> Path:
    """The project root, two levels above ``agent_runtime``."""
    return Path(__file__).resolve().parents[2]


def catalog_candidates() -> list[Path]:
    """Where ``model.json`` may live, in priority order.

    ``AGENT_RUNTIME_MODEL_CATALOG`` is an explicit choice: when it is set
    the path must exist.  Otherwise the working directory's ``model.json``
    wins over the repository's own file.
    """
    raw = os.getenv(CATALOG_ENV, "").strip()
    if raw:
        return [Path(raw).expanduser()]
    return [Path(DEFAULT_CATALOG_NAME), repo_root() / DEFAULT_CATALOG_NAME]


def _required_text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelCatalogError(f"{where} must be non-empty text")
    return value.strip()


def _resolve_secret(value: Any, where: str) -> str:
    text = _required_text(value, where)
    match = _ENV_REF.match(text)
    if match is None:
        return text
    name = match.group(1) or match.group(2)
    resolved = os.getenv(name)
    if not resolved:
        raise ModelCatalogError(
            f"{where} references unset environment variable {name}")
    return resolved


def _entry_from_dict(data: Any, index: int) -> ModelEntry:
    where = f"models[{index}]"
    if not isinstance(data, dict):
        raise ModelCatalogError(f"{where} must be an object")
    allowed = {"id", "label", "base_url", "api_key", "model_id"}
    unknown = set(data) - allowed
    if unknown:
        raise ModelCatalogError(f"unknown {where} keys: {sorted(unknown)}")
    model_id = _required_text(data.get("model_id"), f"{where}.model_id")
    entry_id = _required_text(data.get("id", model_id), f"{where}.id")
    label = data.get("label")
    if label is not None:
        label = _required_text(label, f"{where}.label")
    return ModelEntry(
        id=entry_id,
        label=label or entry_id,
        base_url=_required_text(data.get("base_url"), f"{where}.base_url"),
        api_key=_resolve_secret(data.get("api_key"), f"{where}.api_key"),
        model_id=model_id,
    )


def catalog_from_dict(data: Any) -> ModelCatalog:
    """Validate one decoded ``model.json`` document."""
    if not isinstance(data, dict):
        raise ModelCatalogError("model catalog must be an object")
    unknown = set(data) - {"default", "models"}
    if unknown:
        raise ModelCatalogError(f"unknown catalog keys: {sorted(unknown)}")
    raw_models = data.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        raise ModelCatalogError("models must be a non-empty list")
    entries = tuple(_entry_from_dict(item, index)
                    for index, item in enumerate(raw_models))
    seen: set[str] = set()
    for entry in entries:
        if entry.id in seen:
            raise ModelCatalogError(f"duplicate model id {entry.id!r}")
        seen.add(entry.id)
    default = data.get("default")
    if default is None:
        default = entries[0].id
    default = _required_text(default, "default")
    if default not in seen:
        raise ModelCatalogError(f"default model {default!r} is not in models")
    return ModelCatalog(entries=entries, default=default)


def catalog_from_env() -> ModelCatalog:
    """The implicit single-model catalog built from the environment.

    Keeps deployments that only set ``MODEL_ID`` / ``LLM_BASE_URL`` /
    ``LLM_API_KEY`` working without a ``model.json``.
    """
    from agent_runtime.runtime_config import RuntimeConfig

    runtime = RuntimeConfig.from_env()
    entry = ModelEntry(
        id=runtime.model,
        label=runtime.model,
        base_url=runtime.base_url,
        api_key=runtime.api_key,
        model_id=runtime.model,
    )
    return ModelCatalog(entries=(entry,), default=entry.id)


def load_model_catalog(path: str | Path | None = None) -> ModelCatalog:
    """Load the catalog from ``path`` or the first candidate that exists.

    When no catalog file exists at all, fall back to the environment
    (see :func:`catalog_from_env`).
    """
    candidates = [Path(path)] if path is not None else catalog_candidates()
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ModelCatalogError(
                f"could not read model catalog {candidate}: {exc}") from exc
        catalog = catalog_from_dict(data)
        logger.info("model_catalog.loaded path=%s models=%d",
                    candidate, len(catalog.entries))
        return catalog
    if path is not None:
        raise ModelCatalogError(f"model catalog not found: {path}")
    return catalog_from_env()


__all__ = [
    "CATALOG_ENV",
    "DEFAULT_CATALOG_NAME",
    "ModelCatalog",
    "ModelCatalogError",
    "ModelEntry",
    "catalog_candidates",
    "catalog_from_dict",
    "catalog_from_env",
    "load_model_catalog",
    "repo_root",
]
