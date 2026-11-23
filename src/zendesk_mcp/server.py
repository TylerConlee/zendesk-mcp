"""Builds the MCP server and registers every tool module."""

from __future__ import annotations

import httpx
from mcp.server.mcpserver import MCPServer

from .client import ZendeskClient
from .config import Settings
from .toolkit import Toolkit
from .tools import (admin, advanced, analytics, blueprint, changes, docs, helpcenter, people, reporting,
                    resources, search, tickets)

INSTRUCTIONS = """Full-access Zendesk Support + Help Center server.

Finding things: `search` (Zendesk query syntax) is the most flexible way to locate tickets, users and orgs.
Tickets: get_ticket / list_ticket_comments to read; update_ticket or add_ticket_comment to act
(add_ticket_comment defaults to an INTERNAL note; set public=true to reply to the customer).
Config objects (macros, triggers, automations, views, ticket fields, groups, SLAs, webhooks, Help Center
categories/sections, custom objects...): list_resources / get_resource / create_resource / update_resource /
delete_resource. Anything else: api_request can call any /api/v2 endpoint.
Metrics: support_kpis (FRT, resolution, wait times, one-touch, reopens, CSAT with breakdowns), reply_time_report
(time between every customer message and agent reply, per agent/group), ticket_reply_times (one ticket's
back-and-forth), backlog_report (unsolved now), sla_report (SLA achievement). All times are in minutes;
pass business_hours=true for business-hours figures.
Reports/decks: support_report gathers everything for a review deck in one call (scorecard vs previous period and
targets, trends, topics, CSAT quotes, SLA, heatmap, issues, backlog, Help Center gaps, definitions, slide outline).
Drill down with kpi_trends, ticket_topics, sample_ticket_text (free-text themes), csat_feedback, volume_heatmap,
top_issues, help_center_report. Goals live in report_targets. Periods accept shorthands like last_quarter or 2026-Q3.
For company-wide audiences keep team_level=true and redact=true (the defaults).
Administration: list_agents, provision_agents, offboard_agent (dry run by default); rule_definitions before building
triggers/views/macros/automations; preview_view to test view conditions; reorder; find_references before renaming,
deactivating or deleting anything; account_settings.
Rolling out a configuration (e.g. an architecture plan): config_snapshot first, then express the target state as a
blueprint (blueprint_export shows the current state in the same format), run blueprint_plan, show the user the plan,
and only after they approve run blueprint_apply. Every write is journaled: list_changes shows them, revert_change undoes
one change or a whole batch (a plan id). Prefer a sandbox instance for first runs.
document_config writes a plain-language description of the whole configuration (what every rule does, where each
group/field is used, and findings such as broken references or unused objects) for audits and handovers.
Custom ticket fields are referenced by numeric ID; look them up with list_resources(resource='ticket_fields').
Large results are trimmed; use `limit` and `fields` to keep responses small.
Changes are real and immediate. Confirm with the user before bulk updates, merges, deletes, public replies, agent
provisioning or offboarding, settings changes and blueprint applies."""


def build_server(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> tuple[MCPServer, Toolkit]:
    mcp = MCPServer(
        name="zendesk",
        title="Zendesk",
        version="1.0.0",
        instructions=INSTRUCTIONS + f"\nInstance: {settings.host}. Access mode: {settings.access_mode}.",
    )
    zd = ZendeskClient(settings, transport=transport)
    tk = Toolkit(mcp, zd, settings)
    sets = set(settings.toolsets)
    on = lambda name: "all" in sets or name in sets  # noqa: E731
    modules = []
    if on("core"):
        modules += [search, tickets, people, resources, helpcenter, advanced]
    if on("analytics"):
        modules += [analytics, reporting]
    if on("admin"):
        modules += [admin, blueprint, docs]
    if on("core") or on("admin"):
        modules.append(changes)
    for module in modules:
        module.register(tk)
    return mcp, tk
