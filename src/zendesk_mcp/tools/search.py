"""Unified search across tickets, users, organizations and groups."""

from typing import Annotated, Literal

from pydantic import Field

from ..formatting import project
from ..toolkit import Toolkit
from .tickets import slim_ticket

SEARCH_HELP = """Zendesk search syntax, e.g.:
  type:ticket status<solved assignee:me tags:billing created>2026-09-01
  type:ticket requester:jane@acme.com "refund"
  type:user role:agent email:*@acme.com
  type:organization name:Acme*
  type:ticket custom_field_360001234:some_value  (custom fields)
Operators: field:value, <, >, <=, >=, quotes for phrases, -field:value to exclude,
'*' wildcard (suffix only). Dates: YYYY-MM-DD or relative like created>1week."""


def register(tk: Toolkit) -> None:
    zd = tk.zd

    @tk.tool("read", title="Search Zendesk")
    async def search(
        query: Annotated[str, Field(description=SEARCH_HELP)],
        sort_by: Literal["updated_at", "created_at", "priority", "status", "ticket_type", "relevance"] = "relevance",
        sort_order: Literal["asc", "desc"] = "desc",
        limit: Annotated[int, Field(ge=1, le=10000, description="Over 1000 requires export=true.")] = 25,
        fields: Annotated[list[str] | None, Field(description="Only return these fields per result, e.g. ['id','subject','status'].")] = None,
        export: Annotated[bool, Field(description="Use the export endpoint (no 1000-result cap, no sorting). "
                                      "Requires export_type.")] = False,
        export_type: Literal["ticket", "user", "organization", "group"] | None = None,
        count_only: Annotated[bool, Field(description="Return just the number of matches.")] = False,
    ) -> str:
        """Search tickets, users, organizations and groups with Zendesk's query language.
        This is the most flexible way to find tickets (by status, tags, dates, requester, text, custom fields...)."""
        if count_only:
            return tk.out(await zd.get("search/count", query=query))
        if export:
            if not export_type:
                return tk.out({"error": "export=true requires export_type."})
            page = await zd.paginate("search/export", "results",
                                     params={"query": query, "filter[type]": export_type}, limit=limit)
        else:
            params = {"query": query}
            if sort_by != "relevance":
                params.update(sort_by=sort_by, sort_order=sort_order)
            page = await zd.paginate("search", "results", params=params, limit=min(limit, 1000), cursor=False)
        results = [slim_ticket(r) if r.get("result_type") == "ticket" else r for r in page["results"]]
        page["results"] = project(results, (fields + ["result_type"]) if fields else None)
        return tk.out(page)
