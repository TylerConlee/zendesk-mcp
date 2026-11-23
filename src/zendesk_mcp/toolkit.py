"""Shared plumbing for tool modules: conditional registration by access mode,
MCP tool annotations, and output rendering."""

from __future__ import annotations

from typing import Any, Callable, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .client import ZendeskClient
from .config import Settings
from .formatting import render

Kind = Literal["read", "write", "delete"]


class Toolkit:
    def __init__(self, mcp: MCPServer, zd: ZendeskClient, settings: Settings):
        self.mcp = mcp
        self.zd = zd
        self.settings = settings
        self.registered: list[str] = []
        self.cache: dict[Any, Any] = {}  # short-lived data cache shared by report tools

    def tool(
        self,
        kind: Kind,
        *,
        idempotent: bool = False,
        title: str | None = None,
        read_only_hint: bool | None = None,
        destructive_hint: bool | None = None,
    ) -> Callable:
        """Register a tool unless the access mode disables its kind.

        read   -> always available
        write  -> hidden in read_only mode
        delete -> hidden in read_only and no_delete modes
        The HTTP client enforces the same rules, so hiding is a convenience, not the guard."""
        mode = self.settings.access_mode
        enabled = kind == "read" or (kind == "write" and mode != "read_only") or (kind == "delete" and mode == "full")

        def decorator(fn: Callable) -> Callable:
            if not enabled:
                return fn
            annotations = ToolAnnotations(
                title=title,
                read_only_hint=(kind == "read") if read_only_hint is None else read_only_hint,
                destructive_hint=(kind == "delete") if destructive_hint is None else destructive_hint,
                idempotent_hint=(kind == "read" and read_only_hint is not False) or idempotent,
                open_world_hint=True,
            )
            self.mcp.tool(annotations=annotations, structured_output=False)(fn)
            self.registered.append(fn.__name__)
            return fn

        return decorator

    def out(self, data: Any) -> str:
        return render(data, self.settings.max_response_chars)
