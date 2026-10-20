"""Channel adapters that render agent events for specific frontends."""

from .cli import CLIRenderer
from .webui import WebUIChannel

__all__ = ["CLIRenderer", "WebUIChannel"]
