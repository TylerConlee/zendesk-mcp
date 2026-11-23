"""Configuration loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

AccessMode = Literal["full", "no_delete", "read_only"]
_MODES = ("full", "no_delete", "read_only")


class ConfigError(RuntimeError):
    pass


TOOLSETS = ("core", "analytics", "admin")


def _toolsets(raw: str) -> tuple[str, ...]:
    parts = tuple(p.strip().lower() for p in raw.split(",") if p.strip()) or ("all",)
    bad = [p for p in parts if p not in TOOLSETS + ("all",)]
    if bad:
        raise ConfigError(f"ZENDESK_TOOLSETS: unknown {bad}; choose from all, {', '.join(TOOLSETS)}.")
    return parts


@dataclass(frozen=True)
class Settings:
    host: str  # e.g. "acme.zendesk.com"
    email: str | None
    api_token: str | None
    oauth_token: str | None
    client_id: str | None = None  # OAuth client "Identifier" (client-credentials grant)
    client_secret: str | None = None
    oauth_scope: str = "read write"
    access_mode: AccessMode = "full"
    max_response_chars: int = 60_000
    default_limit: int = 100
    timeout: float = 30.0
    max_retries: int = 4
    file_root: str | None = None  # directory local file upload/download is confined to
    journal: bool = True  # record every write in the change journal
    toolsets: tuple[str, ...] = ("all",)

    @property
    def base_url(self) -> str:
        return f"https://{self.host}/api/v2"

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            from dotenv import find_dotenv, load_dotenv

            load_dotenv(find_dotenv(usecwd=True))  # .env in the working directory; real env vars win
        except ImportError:  # pragma: no cover
            pass

        host = os.getenv("ZENDESK_DOMAIN", "").strip()
        sub = os.getenv("ZENDESK_SUBDOMAIN", "").strip()
        if not host:
            if not sub:
                raise ConfigError("Set ZENDESK_SUBDOMAIN (e.g. 'acme') or ZENDESK_DOMAIN (e.g. 'support.acme.com').")
            host = sub if "." in sub else f"{sub}.zendesk.com"
        host = host.removeprefix("https://").removeprefix("http://").rstrip("/")

        email = os.getenv("ZENDESK_EMAIL") or None
        api_token = os.getenv("ZENDESK_API_TOKEN") or None
        oauth_token = os.getenv("ZENDESK_OAUTH_TOKEN") or None
        client_id = os.getenv("ZENDESK_CLIENT_ID") or None
        client_secret = os.getenv("ZENDESK_CLIENT_SECRET") or None
        if bool(client_id) != bool(client_secret):
            raise ConfigError("Set both ZENDESK_CLIENT_ID and ZENDESK_CLIENT_SECRET for OAuth client credentials.")
        if not (client_id or oauth_token or (email and api_token)):
            raise ConfigError("Set ZENDESK_CLIENT_ID + ZENDESK_CLIENT_SECRET (OAuth client), "
                              "ZENDESK_EMAIL + ZENDESK_API_TOKEN, or ZENDESK_OAUTH_TOKEN.")

        mode = os.getenv("ZENDESK_ACCESS_MODE", "full").strip().lower()
        if mode not in _MODES:
            raise ConfigError(f"ZENDESK_ACCESS_MODE must be one of {_MODES}, got {mode!r}.")

        return cls(
            host=host,
            email=email,
            api_token=api_token,
            oauth_token=oauth_token,
            client_id=client_id,
            client_secret=client_secret,
            oauth_scope=os.getenv("ZENDESK_OAUTH_SCOPE") or ("read" if mode == "read_only" else "read write"),
            access_mode=mode,  # type: ignore[arg-type]
            max_response_chars=int(os.getenv("ZENDESK_MAX_RESPONSE_CHARS", "60000")),
            default_limit=int(os.getenv("ZENDESK_DEFAULT_LIMIT", "100")),
            timeout=float(os.getenv("ZENDESK_TIMEOUT", "30")),
            max_retries=int(os.getenv("ZENDESK_MAX_RETRIES", "4")),
            file_root=os.path.abspath(os.path.expanduser(os.getenv("ZENDESK_FILE_ROOT"))) if os.getenv("ZENDESK_FILE_ROOT") else None,
            journal=os.getenv("ZENDESK_JOURNAL", "on").strip().lower() not in ("0", "off", "false", "no"),
            toolsets=_toolsets(os.getenv("ZENDESK_TOOLSETS", "all")),
        )
