"""Ticket tools: read, create, update, comment, merge, bulk, delete, macros, tags."""

from typing import Annotated, Any, Literal

from pydantic import Field

from ..client import ZendeskError
from ..formatting import project
from ..toolkit import Toolkit

CustomFields = Annotated[
    dict[str, Any] | list[dict[str, Any]] | None,
    Field(description='Custom field values: {"<field_id>": value} or [{"id": <field_id>, "value": value}]. '
          "Use list_resources(resource='ticket_fields') to discover field IDs and allowed values."),
]
Extra = Annotated[
    dict[str, Any] | None,
    Field(description="Any additional raw ticket attributes to merge into the request body "
          "(e.g. {'due_at': '2026-11-01T00:00:00Z', 'problem_id': 123, 'email_ccs': [...]})."),
]
Priority = Literal["urgent", "high", "normal", "low"]
Status = Literal["new", "open", "pending", "hold", "solved", "closed"]
TicketType = Literal["problem", "incident", "question", "task"]


def slim_ticket(t: dict[str, Any]) -> dict[str, Any]:
    """Remove redundant/noisy keys so tickets cost fewer tokens."""
    t = dict(t)
    t.pop("fields", None)  # duplicate of custom_fields
    t.pop("url", None)
    if isinstance(t.get("custom_fields"), list):
        t["custom_fields"] = [f for f in t["custom_fields"] if f.get("value") not in (None, "", [], False)]
    return t


def _custom_fields(cf: dict[str, Any] | list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if cf is None:
        return None
    if isinstance(cf, dict):
        return [{"id": int(k) if str(k).isdigit() else k, "value": v} for k, v in cf.items()]
    return cf


def _comment(body: str | None, html: bool, public: bool, author_id: int | None, upload_tokens: list[str] | None):
    if not body and not upload_tokens:
        return None
    c: dict[str, Any] = {"public": public}
    if body:
        c["html_body" if html else "body"] = body
    if author_id:
        c["author_id"] = author_id
    if upload_tokens:
        c["uploads"] = upload_tokens
    return c


def _ticket_body(**kw: Any) -> dict[str, Any]:
    extra = kw.pop("extra", None) or {}
    body = {k: v for k, v in kw.items() if v is not None}
    body.update(extra)
    return body


def _slim_comment(c: dict[str, Any], body_format: str) -> dict[str, Any]:
    out = {k: c.get(k) for k in ("id", "author_id", "public", "created_at", "via", "type")}
    if body_format in ("plain", "both"):
        out["body"] = c.get("plain_body") or c.get("body")
    if body_format in ("html", "both"):
        out["html_body"] = c.get("html_body")
    if c.get("attachments"):
        out["attachments"] = [
            {k: a.get(k) for k in ("id", "file_name", "content_type", "size", "content_url")} for a in c["attachments"]
        ]
    if c.get("metadata", {}).get("system"):
        sysmeta = c["metadata"]["system"]
        out["client"] = {k: sysmeta.get(k) for k in ("client", "ip_address", "location") if sysmeta.get(k)}
    return out


def _directory(data: dict[str, Any]) -> dict[str, Any]:
    """Collapse sideloaded users/groups/orgs into small id->label maps."""
    d: dict[str, Any] = {}
    if data.get("users"):
        d["users"] = {str(u["id"]): {"name": u.get("name"), "email": u.get("email"), "role": u.get("role")}
                      for u in data["users"]}
    if data.get("groups"):
        d["groups"] = {str(g["id"]): g.get("name") for g in data["groups"]}
    if data.get("organizations"):
        d["organizations"] = {str(o["id"]): o.get("name") for o in data["organizations"]}
    return d


def register(tk: Toolkit) -> None:
    zd = tk.zd

    @tk.tool("read", title="Get ticket")
    async def get_ticket(
        ticket_id: int,
        include_comments: Annotated[int, Field(ge=0, le=200, description="How many most-recent comments to include (0 = none).")] = 10,
        include_metrics: Annotated[bool, Field(description="Include reply/resolution time metrics.")] = False,
        raw: Annotated[bool, Field(description="Return the unmodified API object instead of the slimmed one.")] = False,
    ) -> str:
        """Get a single ticket with requester/assignee/group/org names resolved and,
        optionally, its most recent comments and SLA/time metrics."""
        data = await zd.get(f"tickets/{ticket_id}", include="users,groups,organizations")
        result: dict[str, Any] = {"ticket": data["ticket"] if raw else slim_ticket(data["ticket"])}
        result.update(_directory(data))
        if include_comments:
            page = await zd.paginate(
                f"tickets/{ticket_id}/comments", "comments",
                params={"sort": "-created_at"}, limit=include_comments,
            )
            result["comments_newest_first"] = [_slim_comment(c, "plain") for c in page["comments"]]
            result["more_comments"] = page["has_more"]
        if include_metrics:
            m = await zd.get(f"tickets/{ticket_id}/metrics")
            result["metrics"] = m.get("ticket_metric")
        return tk.out(result)

    @tk.tool("read", title="List tickets")
    async def list_tickets(
        scope: Annotated[
            Literal["all", "requested", "ccd", "assigned", "organization", "view"],
            Field(description="'all' = every ticket; requested/ccd/assigned need a user id; "
                  "organization needs an org id; view needs a view id."),
        ] = "all",
        id: Annotated[int | None, Field(description="User, organization or view ID required by the scope.")] = None,
        limit: Annotated[int, Field(ge=1, le=5000)] = 50,
        sort: Annotated[str | None, Field(description="e.g. 'updated_at' or '-created_at' (cursor sort).")] = None,
        fields: Annotated[list[str] | None, Field(description="Only return these ticket fields, e.g. ['id','subject','status','updated_at'].")] = None,
    ) -> str:
        """List tickets by scope. For filtering by status, tags, dates, text etc. use `search`
        with type:ticket instead (it is far more flexible)."""
        paths = {
            "all": "tickets",
            "requested": "users/{id}/tickets/requested",
            "ccd": "users/{id}/tickets/ccd",
            "assigned": "users/{id}/tickets/assigned",
            "organization": "organizations/{id}/tickets",
            "view": "views/{id}/tickets",
        }
        if scope != "all" and id is None:
            raise ZendeskError(f"scope={scope!r} requires `id`.")
        params = {"sort": sort} if sort else None
        page = await zd.paginate(paths[scope].format(id=id), "tickets", params=params, limit=limit)
        page["tickets"] = project((slim_ticket(t) for t in page["tickets"]), fields)
        return tk.out(page)

    @tk.tool("write", title="Create ticket")
    async def create_ticket(
        subject: str,
        comment: Annotated[str, Field(description="First comment / description (plain text, or HTML if html=true).")],
        requester_id: int | None = None,
        requester_email: Annotated[str | None, Field(description="Used (with requester_name) if requester_id not given; creates the user if needed.")] = None,
        requester_name: str | None = None,
        public: Annotated[bool, Field(description="Whether the first comment is public.")] = True,
        html: bool = False,
        assignee_id: int | None = None,
        assignee_email: str | None = None,
        group_id: int | None = None,
        organization_id: int | None = None,
        priority: Priority | None = None,
        type: TicketType | None = None,
        status: Status | None = None,
        tags: list[str] | None = None,
        custom_fields: CustomFields = None,
        ticket_form_id: int | None = None,
        brand_id: int | None = None,
        collaborator_ids: list[int] | None = None,
        external_id: str | None = None,
        upload_tokens: Annotated[list[str] | None, Field(description="Tokens from upload_attachment to attach to the first comment.")] = None,
        extra: Extra = None,
    ) -> str:
        """Create a ticket."""
        requester = None
        if not requester_id and requester_email:
            requester = {"email": requester_email, "name": requester_name or requester_email.split("@")[0]}
        ticket = _ticket_body(
            subject=subject,
            comment=_comment(comment, html, public, None, upload_tokens),
            requester_id=requester_id, requester=requester, assignee_id=assignee_id, assignee_email=assignee_email,
            group_id=group_id, organization_id=organization_id, priority=priority, type=type, status=status,
            tags=tags, custom_fields=_custom_fields(custom_fields), ticket_form_id=ticket_form_id,
            brand_id=brand_id, collaborator_ids=collaborator_ids, external_id=external_id, extra=extra,
        )
        data = await zd.request("POST", "tickets", json_body={"ticket": ticket})
        return tk.out({"ticket": slim_ticket(data["ticket"])})

    @tk.tool("write", title="Update ticket")
    async def update_ticket(
        ticket_id: int,
        comment: Annotated[str | None, Field(description="Optional comment to add with the update.")] = None,
        public: Annotated[bool, Field(description="Comment visibility: true = public reply, false = internal note.")] = False,
        html: bool = False,
        author_id: Annotated[int | None, Field(description="Post the comment as this user (defaults to the API user).")] = None,
        subject: str | None = None,
        status: Status | None = None,
        custom_status_id: int | None = None,
        priority: Priority | None = None,
        type: TicketType | None = None,
        assignee_id: int | None = None,
        assignee_email: str | None = None,
        group_id: int | None = None,
        requester_id: int | None = None,
        organization_id: int | None = None,
        tags: Annotated[list[str] | None, Field(description="REPLACES all tags. Use additional_tags/remove_tags to modify.")] = None,
        additional_tags: list[str] | None = None,
        remove_tags: list[str] | None = None,
        custom_fields: CustomFields = None,
        ticket_form_id: int | None = None,
        due_at: str | None = None,
        upload_tokens: list[str] | None = None,
        safe_update_timestamp: Annotated[str | None, Field(description="Ticket's updated_at; if set, the update fails on a concurrent edit instead of overwriting.")] = None,
        extra: Extra = None,
    ) -> str:
        """Update ticket properties and/or add a public reply or internal note."""
        ticket = _ticket_body(
            comment=_comment(comment, html, public, author_id, upload_tokens),
            subject=subject, status=status, custom_status_id=custom_status_id, priority=priority, type=type,
            assignee_id=assignee_id, assignee_email=assignee_email, group_id=group_id, requester_id=requester_id,
            organization_id=organization_id, tags=tags, additional_tags=additional_tags, remove_tags=remove_tags,
            custom_fields=_custom_fields(custom_fields), ticket_form_id=ticket_form_id, due_at=due_at, extra=extra,
        )
        if safe_update_timestamp:
            ticket.update(safe_update=True, updated_stamp=safe_update_timestamp)
        if not ticket:
            raise ZendeskError("Nothing to update: pass at least one field or a comment.")
        data = await zd.request("PUT", f"tickets/{ticket_id}", json_body={"ticket": ticket})
        return tk.out({"ticket": slim_ticket(data["ticket"])})

    @tk.tool("write", title="Add comment / reply")
    async def add_ticket_comment(
        ticket_id: int,
        body: str,
        public: Annotated[bool, Field(description="true = public reply visible to requester; false = internal note.")] = False,
        html: bool = False,
        author_id: int | None = None,
        status: Annotated[Status | None, Field(description="Optionally change status in the same update (e.g. 'pending').")] = None,
        upload_tokens: list[str] | None = None,
    ) -> str:
        """Add a public reply or internal note to a ticket. Defaults to an internal note for safety."""
        ticket = _ticket_body(comment=_comment(body, html, public, author_id, upload_tokens), status=status)
        data = await zd.request("PUT", f"tickets/{ticket_id}", json_body={"ticket": ticket})
        t = data["ticket"]
        return tk.out({"ok": True, "ticket_id": t["id"], "status": t.get("status"), "updated_at": t.get("updated_at"),
                       "comment_public": public})

    @tk.tool("read", title="List ticket comments")
    async def list_ticket_comments(
        ticket_id: int,
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
        newest_first: bool = False,
        body_format: Literal["plain", "html", "both"] = "plain",
    ) -> str:
        """List a ticket's comments (conversation), with attachment metadata."""
        page = await zd.paginate(
            f"tickets/{ticket_id}/comments", "comments",
            params={"sort": "-created_at" if newest_first else "created_at", "include": "users"}, limit=limit,
        )
        result = {
            "comments": [_slim_comment(c, body_format) for c in page["comments"]],
            "returned": page["returned"], "has_more": page["has_more"],
        }
        result.update(_directory(page))
        return tk.out(result)

    @tk.tool("read", title="List ticket audits")
    async def list_ticket_audits(ticket_id: int, limit: Annotated[int, Field(ge=1, le=1000)] = 50) -> str:
        """Full change history of a ticket (every field change, trigger/automation firing, notification)."""
        return tk.out(await zd.paginate(f"tickets/{ticket_id}/audits", "audits", limit=limit))

    @tk.tool("write", title="Bulk update tickets")
    async def bulk_update_tickets(
        ticket_ids: Annotated[list[int], Field(min_length=1, max_length=100)],
        status: Status | None = None,
        priority: Priority | None = None,
        type: TicketType | None = None,
        assignee_id: int | None = None,
        group_id: int | None = None,
        additional_tags: list[str] | None = None,
        remove_tags: list[str] | None = None,
        custom_fields: CustomFields = None,
        comment: str | None = None,
        public: bool = False,
        extra: Extra = None,
    ) -> str:
        """Apply the same change to up to 100 tickets. Runs as a background job;
        poll with get_resource(resource='job_statuses', id=...)."""
        ticket = _ticket_body(
            status=status, priority=priority, type=type, assignee_id=assignee_id, group_id=group_id,
            additional_tags=additional_tags, remove_tags=remove_tags, custom_fields=_custom_fields(custom_fields),
            comment=_comment(comment, False, public, None, None), extra=extra,
        )
        if not ticket:
            raise ZendeskError("Nothing to update.")
        data = await zd.request("PUT", "tickets/update_many", params={"ids": ",".join(map(str, ticket_ids))},
                                json_body={"ticket": ticket})
        return tk.out(data)

    @tk.tool("write", title="Merge tickets")
    async def merge_tickets(
        target_ticket_id: int,
        source_ticket_ids: Annotated[list[int], Field(min_length=1, max_length=5)],
        target_comment: str | None = None,
        source_comment: str | None = None,
        public: bool = False,
    ) -> str:
        """Merge source tickets into a target ticket. Sources are closed. This cannot be undone."""
        body = {k: v for k, v in {
            "ids": source_ticket_ids, "target_comment": target_comment, "source_comment": source_comment,
            "target_comment_is_public": public if target_comment else None,
            "source_comment_is_public": public if source_comment else None,
        }.items() if v is not None}
        return tk.out(await zd.request("POST", f"tickets/{target_ticket_id}/merge", json_body=body))

    @tk.tool("delete", title="Delete tickets")
    async def delete_tickets(ticket_ids: Annotated[list[int], Field(min_length=1, max_length=100)]) -> str:
        """Soft-delete tickets (recoverable for ~30 days via
        api_request PUT deleted_tickets/restore_many?ids=...)."""
        if len(ticket_ids) == 1:
            return tk.out(await zd.request("DELETE", f"tickets/{ticket_ids[0]}"))
        return tk.out(await zd.request("DELETE", "tickets/destroy_many", params={"ids": ",".join(map(str, ticket_ids))}))

    @tk.tool("write", title="Apply macro")
    async def apply_macro(
        ticket_id: int,
        macro_id: int,
        preview_only: Annotated[bool, Field(description="true = just show what the macro would change.")] = False,
    ) -> str:
        """Apply a macro to a ticket (or preview its effect). Find macros with
        list_resources(resource='macros') or search via api_request GET macros/search?query=..."""
        preview = await zd.get(f"tickets/{ticket_id}/macros/{macro_id}/apply")
        changes = preview.get("result", {})
        if preview_only:
            return tk.out({"preview": changes})
        ticket = dict(changes.get("ticket") or {})
        ticket.pop("id", None)
        ticket.pop("url", None)
        comment = changes.get("comment")
        if comment:
            ticket["comment"] = {k: comment[k] for k in ("body", "html_body", "public", "uploads") if comment.get(k) is not None}
            if "html_body" in ticket["comment"]:
                ticket["comment"].pop("body", None)
        data = await zd.request("PUT", f"tickets/{ticket_id}", json_body={"ticket": ticket})
        return tk.out({"applied": True, "ticket": slim_ticket(data["ticket"])})

    @tk.tool("write", title="Manage tags", idempotent=True)
    async def manage_tags(
        target: Literal["tickets", "users", "organizations"],
        id: int,
        action: Annotated[Literal["get", "add", "remove", "set"], Field(description="'set' replaces all tags.")],
        tags: list[str] | None = None,
    ) -> str:
        """Get, add, remove or replace tags on a ticket, user or organization."""
        path = f"{target}/{id}/tags"
        if action == "get":
            return tk.out(await zd.get(path))
        if not tags:
            raise ZendeskError("`tags` is required for add/remove/set.")
        method = {"add": "PUT", "set": "POST", "remove": "DELETE"}[action]
        # Removing tags uses HTTP DELETE but only edits tags, so treat it as a write for access mode.
        return tk.out(await zd.request(method, path, json_body={"tags": tags}, access_method="PUT"))
