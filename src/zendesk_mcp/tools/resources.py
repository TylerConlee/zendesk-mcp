"""Generic CRUD over Zendesk's configuration and directory objects.

One set of five tools covers ~40 resource types instead of ~200 near-identical
tools, which keeps the tool list small enough for the model to reason about."""

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import Field

from ..client import ZendeskError
from ..formatting import project
from ..toolkit import Toolkit


@dataclass(frozen=True)
class Res:
    path: str  # list/create path; may contain {parent_id}
    singular: str  # request/response wrapper key for one object
    plural: str  # response key for lists
    read_only: bool = False
    cursor: bool = True  # supports page[size] cursor pagination
    create_path: str | None = None  # if creating needs a different (parent-scoped) path
    item_path: str | None = None  # if get/update/delete differ from f"{path}/{id}"
    note: str = ""


RESOURCES: dict[str, Res] = {
    # Directory
    "users": Res("users", "user", "users"),
    "organizations": Res("organizations", "organization", "organizations"),
    "groups": Res("groups", "group", "groups"),
    "group_memberships": Res("group_memberships", "group_membership", "group_memberships"),
    "organization_memberships": Res("organization_memberships", "organization_membership", "organization_memberships"),
    "user_identities": Res("users/{parent_id}/identities", "identity", "identities", note="parent_id = user id"),
    "custom_roles": Res("custom_roles", "custom_role", "custom_roles", cursor=False,
                        note="Enterprise; e.g. light agent roles"),
    # Ticket configuration
    "ticket_fields": Res("ticket_fields", "ticket_field", "ticket_fields"),
    "ticket_field_options": Res("ticket_fields/{parent_id}/options", "custom_field_option", "custom_field_options",
                                note="parent_id = ticket field id"),
    "ticket_forms": Res("ticket_forms", "ticket_form", "ticket_forms", cursor=False),
    "custom_statuses": Res("custom_statuses", "custom_status", "custom_statuses", cursor=False),
    "user_fields": Res("user_fields", "user_field", "user_fields"),
    "user_field_options": Res("user_fields/{parent_id}/options", "custom_field_option", "custom_field_options",
                              note="parent_id = user field id"),
    "organization_fields": Res("organization_fields", "organization_field", "organization_fields"),
    "brands": Res("brands", "brand", "brands"),
    "tags": Res("tags", "tag", "tags", read_only=True, note="account-wide tag list with counts"),
    # Business rules
    "macros": Res("macros", "macro", "macros"),
    "triggers": Res("triggers", "trigger", "triggers"),
    "trigger_categories": Res("trigger_categories", "trigger_category", "trigger_categories"),
    "automations": Res("automations", "automation", "automations"),
    "views": Res("views", "view", "views"),
    "sla_policies": Res("slas/policies", "sla_policy", "sla_policies", cursor=False),
    "group_sla_policies": Res("group_slas/policies", "group_sla_policy", "group_sla_policies", cursor=False),
    "schedules": Res("business_hours/schedules", "schedule", "schedules", cursor=False),
    "schedule_holidays": Res("business_hours/schedules/{parent_id}/holidays", "holiday", "holidays", cursor=False,
                             note="parent_id = schedule id"),
    "dynamic_content_items": Res("dynamic_content/items", "item", "items"),
    "targets": Res("targets", "target", "targets", cursor=False),
    "webhooks": Res("webhooks", "webhook", "webhooks"),
    # Routing
    "routing_attributes": Res("routing/attributes", "attribute", "attributes", cursor=False,
                              note="skills, e.g. Language"),
    "routing_attribute_values": Res("routing/attributes/{parent_id}/values", "attribute_value", "attribute_values",
                                    cursor=False, note="parent_id = routing attribute id"),
    "queues": Res("queues", "queue", "queues", cursor=False,
                  note="omnichannel routing; create uses primary_groups_id/secondary_groups_id"),
    "recipient_addresses": Res("recipient_addresses", "recipient_address", "recipient_addresses"),
    "sharing_agreements": Res("sharing_agreements", "sharing_agreement", "sharing_agreements", cursor=False),
    "custom_objects": Res("custom_objects", "custom_object", "custom_objects", cursor=False,
                          note="id = custom object key"),
    "custom_object_records": Res("custom_objects/{parent_id}/records", "custom_object_record",
                                 "custom_object_records", note="parent_id = custom object key"),
    # Ticket-adjacent
    "suspended_tickets": Res("suspended_tickets", "suspended_ticket", "suspended_tickets",
                             note="recover via api_request PUT suspended_tickets/recover_many?ids=..."),
    "deleted_tickets": Res("deleted_tickets", "deleted_ticket", "deleted_tickets", read_only=True),
    "ticket_metrics": Res("ticket_metrics", "ticket_metric", "ticket_metrics", read_only=True),
    "satisfaction_ratings": Res("satisfaction_ratings", "satisfaction_rating", "satisfaction_ratings", read_only=True),
    "satisfaction_reasons": Res("satisfaction_reasons", "reason", "reasons", read_only=True),
    "side_conversations": Res("tickets/{parent_id}/side_conversations", "side_conversation", "side_conversations",
                              cursor=False, note="parent_id = ticket id"),
    "job_statuses": Res("job_statuses", "job_status", "job_statuses", read_only=True),
    "audit_logs": Res("audit_logs", "audit_log", "audit_logs", read_only=True),
    "activities": Res("activities", "activity", "activities", read_only=True),
    "locales": Res("locales", "locale", "locales", read_only=True, cursor=False),
    # Help Center
    "help_center_categories": Res("help_center/categories", "category", "categories"),
    "help_center_sections": Res("help_center/sections", "section", "sections",
                                create_path="help_center/categories/{parent_id}/sections",
                                note="create: parent_id = category id"),
    "help_center_articles": Res("help_center/articles", "article", "articles",
                                create_path="help_center/sections/{parent_id}/articles",
                                note="create: parent_id = section id. Prefer the dedicated article tools."),
    "help_center_topics": Res("community/topics", "topic", "topics"),
    "help_center_posts": Res("community/posts", "post", "posts"),
    "help_center_user_segments": Res("help_center/user_segments", "user_segment", "user_segments"),
    "help_center_permission_groups": Res("guide/permission_groups", "permission_group", "permission_groups"),
}

ResourceName = Literal[tuple(RESOURCES)]  # type: ignore[valid-type]

RESOURCE_DOC = "Resource type. Ones needing parent_id: " + ", ".join(
    f"{k} ({v.note})" for k, v in RESOURCES.items() if "{parent_id}" in v.path or v.create_path
) + ". Read-only: " + ", ".join(k for k, v in RESOURCES.items() if v.read_only) + "."

Rid = Annotated[int | str, Field(description="Object ID (or key for custom_objects).")]
ParentId = Annotated[int | str | None, Field(description="Parent ID for nested resources (see resource docs).")]


def _fill(path: str, parent_id: Any, name: str) -> str:
    if "{parent_id}" in path:
        if parent_id is None:
            raise ZendeskError(f"Resource {name!r} requires parent_id ({RESOURCES[name].note}).")
        return path.format(parent_id=parent_id)
    return path


def _get(name: str) -> Res:
    try:
        return RESOURCES[name]
    except KeyError:
        raise ZendeskError(f"Unknown resource {name!r}. Valid: {', '.join(RESOURCES)}") from None


def _writable(name: str) -> Res:
    r = _get(name)
    if r.read_only:
        raise ZendeskError(f"Resource {name!r} is read-only through this tool; use api_request if an endpoint exists.")
    return r


async def fetch_before(tk: Toolkit, path: str, wrapper: str, force: bool = False) -> dict | None:
    """Best-effort capture of an object's current state so the change can be reverted."""
    if not tk.zd.journal.enabled and not force:
        return None
    try:
        body = await tk.zd.request("GET", path)
    except ZendeskError:
        return None
    obj = body.get(wrapper) if isinstance(body, dict) else None
    return obj if isinstance(obj, dict) else None


GUARDED = ("triggers", "automations", "views", "ticket_fields", "user_fields", "organization_fields")


def guard_update(resource: str, data: dict[str, Any], before: dict | None, allow_option_removal: bool) -> tuple[dict, list[str]]:
    """Protect against Zendesk's replace-whole-array semantics. Returns (safe payload, notes)."""
    data = dict(data)
    notes: list[str] = []
    if before is None:
        raise ZendeskError(f"Couldn't read the current {resource[:-1]} to update it safely; try again.")
    if resource in ("triggers", "automations", "views"):
        live = before.get("conditions") or {}
        if "all" in data or "any" in data:
            cond = {"all": data.pop("all", None), "any": data.pop("any", None)}
        elif isinstance(data.get("conditions"), dict):
            cond = dict(data.pop("conditions"))
        else:
            cond = None
        if cond is not None:
            for bucket in ("all", "any"):
                if cond.get(bucket) is None:
                    cond[bucket] = live.get(bucket) or []
                    notes.append(f"kept the existing `{bucket}` conditions (Zendesk replaces all conditions on update)")
            if resource == "views":
                data["all"], data["any"] = cond["all"], cond["any"]
            else:
                data["conditions"] = cond
        if resource in ("triggers", "automations") and (("conditions" in data) != ("actions" in data)):
            missing = "actions" if "conditions" in data else "conditions"
            data[missing] = before.get(missing) or ([] if missing == "actions" else {"all": [], "any": []})
            notes.append(f"included the existing {missing} (Zendesk clears both when either is updated)")
    if resource in ("ticket_fields", "user_fields", "organization_fields") and "custom_field_options" in data:
        new = data["custom_field_options"] or []
        new_ids = {str(o.get("id")) for o in new if o.get("id") is not None}
        new_vals = {str(o.get("value")).lower() for o in new if o.get("value") is not None}
        dropped = [o.get("name") for o in before.get("custom_field_options") or []
                   if str(o.get("id")) not in new_ids and str(o.get("value")).lower() not in new_vals]
        if dropped and not allow_option_removal:
            raise ZendeskError(
                f"This update would delete options {dropped}: Zendesk replaces the whole option list and removes "
                "deleted options' values from every ticket and macro. Include them to keep them, or pass "
                "allow_option_removal=true if removing them is intended.")
        if dropped:
            notes.append(f"removed options {dropped}")
    return data, notes


def with_change(result: Any, change_id: str | None) -> Any:
    if change_id and isinstance(result, dict):
        return {**result, "change_id": change_id}
    return result


def register(tk: Toolkit) -> None:
    zd = tk.zd

    @tk.tool("read", title="List resources")
    async def list_resources(
        resource: Annotated[ResourceName, Field(description=RESOURCE_DOC)],
        parent_id: ParentId = None,
        params: Annotated[dict[str, Any] | None, Field(description="Extra query params, e.g. {'active': true} for macros, "
                                                       "{'role': 'agent'} for users, {'sort_by': 'position'}.")] = None,
        limit: Annotated[int, Field(ge=1, le=5000)] = 100,
        fields: Annotated[list[str] | None, Field(description="Only return these fields (dot paths ok), e.g. ['id','title','active'].")] = None,
    ) -> str:
        """List objects of a resource type (macros, triggers, views, ticket fields, groups, users, Help Center
        sections, webhooks, SLA policies, etc.)."""
        r = _get(resource)
        page = await zd.paginate(_fill(r.path, parent_id, resource), r.plural, params=params, limit=limit, cursor=r.cursor)
        page[r.plural] = project(page[r.plural], fields)
        return tk.out(page)

    @tk.tool("read", title="Get resource")
    async def get_resource(
        resource: Annotated[ResourceName, Field(description=RESOURCE_DOC)],
        id: Rid,
        parent_id: ParentId = None,
        params: Annotated[dict[str, Any] | None, Field(description="Extra query params, e.g. {'include': 'permissions'}.")] = None,
    ) -> str:
        """Get one object by ID."""
        r = _get(resource)
        base = _fill(r.item_path or r.path, parent_id, resource)
        return tk.out(await zd.request("GET", f"{base}/{id}", params=params))

    @tk.tool("write", title="Create resource")
    async def create_resource(
        resource: Annotated[ResourceName, Field(description=RESOURCE_DOC)],
        data: Annotated[dict[str, Any], Field(description="Object attributes (unwrapped), e.g. for a macro "
                                              "{'title':'Close','actions':[{'field':'status','value':'solved'}]}.")],
        parent_id: ParentId = None,
    ) -> str:
        """Create an object (macro, trigger, automation, view, group, user, organization, ticket field, webhook,
        Help Center category/section, etc.)."""
        r = _writable(resource)
        path = _fill(r.create_path or r.path, parent_id, resource)
        body, change = await zd.write("POST", path, json_body={r.singular: data}, wrapper=r.singular)
        return tk.out(with_change(body, change))

    @tk.tool("write", title="Update resource", idempotent=True)
    async def update_resource(
        resource: Annotated[ResourceName, Field(description=RESOURCE_DOC)],
        id: Rid,
        data: Annotated[dict[str, Any], Field(description="Attributes to change (unwrapped). Arrays such as "
                                              "actions or a view's columns replace the existing array; for triggers, "
                                              "automations and views the server keeps whatever you don't send.")],
        parent_id: ParentId = None,
        allow_option_removal: Annotated[bool, Field(description="Allow a field update that drops dropdown options "
                                                    "(removes their values from tickets and macros).")] = False,
    ) -> str:
        """Update an object by ID. For triggers/automations/views, unspecified conditions or actions are preserved
        (Zendesk would otherwise clear them); dropping dropdown options requires allow_option_removal."""
        r = _writable(resource)
        base = _fill(r.item_path or r.path, parent_id, resource)
        method = "PATCH" if resource in ("webhooks", "custom_objects", "custom_object_records",
                                         "trigger_categories") else "PUT"
        before = await fetch_before(tk, f"{base}/{id}", r.singular, force=resource in GUARDED)
        notes: list[str] = []
        if resource in GUARDED:
            data, notes = guard_update(resource, data, before, allow_option_removal)
        body, change = await zd.write(method, f"{base}/{id}", json_body={r.singular: data}, before=before,
                                      wrapper=r.singular)
        result = with_change(body, change)
        if notes and isinstance(result, dict):
            result["notes"] = notes
        return tk.out(result)

    @tk.tool("delete", title="Delete resource")
    async def delete_resource(
        resource: Annotated[ResourceName, Field(description=RESOURCE_DOC)],
        id: Rid,
        parent_id: ParentId = None,
    ) -> str:
        """Delete an object by ID. Most deletions are permanent; the object's last state is kept in the change
        journal so revert_change can re-create it (with a new ID)."""
        r = _writable(resource)
        base = _fill(r.item_path or r.path, parent_id, resource)
        before = await fetch_before(tk, f"{base}/{id}", r.singular)
        body, change = await zd.write("DELETE", f"{base}/{id}", before=before, wrapper=r.singular)
        return tk.out(with_change(body, change))
