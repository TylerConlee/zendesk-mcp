import base64
import json

import httpx
import pytest

from conftest import BASE, HOST, data, text
from zendesk_mcp.client import ZendeskClient, ZendeskError
from zendesk_mcp.formatting import render

from conftest import make_settings

TICKET = {
    "id": 42, "subject": "Printer on fire", "status": "open", "requester_id": 7, "assignee_id": 8, "group_id": 3,
    "url": f"{BASE}/tickets/42.json", "fields": [{"id": 1, "value": None}],
    "custom_fields": [{"id": 1, "value": None}, {"id": 2, "value": "gold"}], "tags": ["fire"], "due_at": None,
}


# ---------------------------------------------------------------- registration
async def test_tool_counts_by_mode(connect):
    async with connect() as c:
        full = {t.name for t in (await c.list_tools()).tools}
    async with connect(access_mode="no_delete") as c:
        nodel = {t.name for t in (await c.list_tools()).tools}
    async with connect(access_mode="read_only") as c:
        ro = {t.name for t in (await c.list_tools()).tools}
    assert len(full) == 72
    assert full - nodel == {"delete_tickets", "delete_resource"}
    assert "update_ticket" not in ro and "create_resource" not in ro
    assert {"search", "get_ticket", "api_request", "list_resources", "download_attachment", "customer_profile"} <= ro


async def test_annotations(connect):
    async with connect() as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert tools["get_ticket"].annotations.read_only_hint is True
    assert tools["delete_tickets"].annotations.destructive_hint is True
    assert tools["api_request"].annotations.read_only_hint is False


# ---------------------------------------------------------------- tickets
async def test_get_ticket_slims_and_resolves_names(connect, fake):
    fake.on("GET", "/api/v2/tickets/42", {
        "ticket": TICKET,
        "users": [{"id": 7, "name": "Jane", "email": "jane@x.com", "role": "end-user"}],
        "groups": [{"id": 3, "name": "Tier 2"}],
    })
    fake.on("GET", "/api/v2/tickets/42/comments", {
        "comments": [{"id": 1, "author_id": 7, "public": True, "plain_body": "Help!", "body": "Help!",
                      "html_body": "<p>Help!</p>", "attachments": [{"id": 99, "file_name": "a.png",
                      "content_type": "image/png", "size": 10, "content_url": "x", "thumbnails": []}]}],
        "meta": {"has_more": False}, "links": {},
    })
    async with connect() as c:
        out = data(await c.call_tool("get_ticket", {"ticket_id": 42, "include_comments": 5}))
    t = out["ticket"]
    assert "fields" not in t and "url" not in t and "due_at" not in t
    assert t["custom_fields"] == [{"id": 2, "value": "gold"}]
    assert out["users"]["7"]["name"] == "Jane" and out["groups"]["3"] == "Tier 2"
    assert out["comments_newest_first"][0]["body"] == "Help!"
    assert out["comments_newest_first"][0]["attachments"][0]["id"] == 99
    comment_req = fake.calls[1]
    assert comment_req.url.params["sort"] == "-created_at" and comment_req.url.params["page[size]"] == "5"


async def test_create_ticket_body(connect, fake):
    fake.on("POST", "/api/v2/tickets", lambda r: {"ticket": {**TICKET, "id": 100}}, status=201)
    async with connect() as c:
        out = data(await c.call_tool("create_ticket", {
            "subject": "Hi", "comment": "Body", "requester_email": "new@x.com", "priority": "high",
            "custom_fields": {"360001": "abc"}, "public": False, "extra": {"due_at": "2026-11-01T00:00:00Z"},
        }))
    body = fake.body()["ticket"]
    assert body["requester"] == {"email": "new@x.com", "name": "new"}
    assert body["comment"] == {"public": False, "body": "Body"}
    assert body["custom_fields"] == [{"id": 360001, "value": "abc"}]
    assert body["due_at"] == "2026-11-01T00:00:00Z" and "assignee_id" not in body
    assert out["ticket"]["id"] == 100
    assert fake.calls[-1].headers["authorization"].startswith("Basic ")


async def test_add_comment_defaults_to_internal(connect, fake):
    fake.on("PUT", "/api/v2/tickets/42", {"ticket": TICKET})
    async with connect() as c:
        out = data(await c.call_tool("add_ticket_comment", {"ticket_id": 42, "body": "note", "status": "pending"}))
    assert fake.body() == {"ticket": {"comment": {"public": False, "body": "note"}, "status": "pending"}}
    assert out["comment_public"] is False


async def test_update_ticket_requires_something(connect, fake):
    async with connect() as c:
        r = await c.call_tool("update_ticket", {"ticket_id": 42})
    assert r.is_error and "Nothing to update" in text(r)
    assert not fake.calls


async def test_bulk_update_and_merge(connect, fake):
    fake.on("PUT", "/api/v2/tickets/update_many", {"job_status": {"id": "j1", "status": "queued"}})
    fake.on("POST", "/api/v2/tickets/1/merge", {"job_status": {"id": "j2"}})
    async with connect() as c:
        data(await c.call_tool("bulk_update_tickets", {"ticket_ids": [1, 2, 3], "status": "solved",
                                                       "additional_tags": ["x"]}))
        assert fake.calls[-1].url.params["ids"] == "1,2,3"
        assert fake.body() == {"ticket": {"status": "solved", "additional_tags": ["x"]}}
        data(await c.call_tool("merge_tickets", {"target_ticket_id": 1, "source_ticket_ids": [2],
                                                 "target_comment": "merged"}))
        assert fake.body() == {"ids": [2], "target_comment": "merged", "target_comment_is_public": False}


async def test_apply_macro(connect, fake):
    fake.on("GET", "/api/v2/tickets/42/macros/5/apply", {"result": {
        "ticket": {"id": 42, "status": "solved", "url": "x"},
        "comment": {"body": "Thanks", "html_body": "<p>Thanks</p>", "public": True, "scoped_body": []}}})
    fake.on("PUT", "/api/v2/tickets/42", {"ticket": TICKET})
    async with connect() as c:
        data(await c.call_tool("apply_macro", {"ticket_id": 42, "macro_id": 5}))
    assert fake.body() == {"ticket": {"status": "solved", "comment": {"html_body": "<p>Thanks</p>", "public": True}}}


# ---------------------------------------------------------------- pagination
async def test_cursor_pagination_respects_limit(connect, fake):
    page2 = f"{BASE}/macros.json?page[after]=abc&page[size]=100"
    calls = {"n": 0}

    def macros(req):
        calls["n"] += 1
        if "page[after]" in str(req.url):
            return {"macros": [{"id": i, "title": f"m{i}", "actions": []} for i in range(100, 200)],
                    "meta": {"has_more": True}, "links": {"next": f"{BASE}/macros.json?page[after]=def"}}
        return {"macros": [{"id": i, "title": f"m{i}", "actions": []} for i in range(100)],
                "meta": {"has_more": True}, "links": {"next": page2}}

    fake.on("GET", "/api/v2/macros.json", macros)
    fake.on("GET", "/api/v2/macros", macros)
    async with connect() as c:
        out = data(await c.call_tool("list_resources", {"resource": "macros", "limit": 150, "fields": ["id", "title"]}))
    assert out["returned"] == 150 and out["has_more"] is True
    assert out["macros"][149] == {"id": 149, "title": "m149"}
    assert calls["n"] == 2


async def test_search_offset_pagination(connect, fake):
    def search(req):
        p = int(req.url.params.get("page", 1))
        nxt = f"{BASE}/search.json?page={p + 1}&query=x" if p < 3 else None
        return {"results": [{"id": p * 10 + i, "result_type": "ticket", "subject": "s", "fields": []} for i in range(2)],
                "next_page": nxt, "count": 6}

    fake.on("GET", "/api/v2/search", search)
    fake.on("GET", "/api/v2/search.json", search)
    async with connect() as c:
        out = data(await c.call_tool("search", {"query": "type:ticket status:open", "limit": 100,
                                                "fields": ["id"], "sort_by": "updated_at"}))
    assert [r["id"] for r in out["results"]] == [10, 11, 20, 21, 30, 31]
    assert out["total_count"] == 6 and out["has_more"] is False
    first = fake.calls[0].url.params
    assert first["per_page"] == "100" and first["sort_by"] == "updated_at" and "page[size]" not in first


# ---------------------------------------------------------------- access modes & safety
async def test_read_only_blocks_writes_before_network(connect, fake):
    async with connect(access_mode="read_only") as c:
        r = await c.call_tool("api_request", {"method": "POST", "path": "tickets", "body": {"ticket": {}}})
    assert r.is_error and "read_only" in text(r)
    assert not fake.calls


async def test_no_delete_blocks_delete_but_allows_tag_removal(connect, fake):
    fake.on("DELETE", "/api/v2/tickets/1/tags", {"tags": []})
    async with connect(access_mode="no_delete") as c:
        r = await c.call_tool("api_request", {"method": "DELETE", "path": "/api/v2/tickets/1.json"})
        assert r.is_error and "no_delete" in text(r)
        data(await c.call_tool("manage_tags", {"target": "tickets", "id": 1, "action": "remove", "tags": ["a"]}))
    assert len(fake.calls) == 1


async def test_foreign_host_refused():
    zd = ZendeskClient(make_settings())
    with pytest.raises(ZendeskError, match="Refusing"):
        zd.normalize_path("https://evil.example.com/api/v2/tickets")
    with pytest.raises(ZendeskError, match="traversal"):
        zd.normalize_path("tickets/../../admin")
    assert zd.normalize_path(f"https://{HOST}/api/v2/tickets.json?page=2") == "tickets.json?page=2"


async def test_api_error_surfaces_details(connect, fake):
    fake.on("PUT", "/api/v2/tickets/9", {"error": "RecordInvalid", "description": "Record validation errors",
                                         "details": {"status": [{"description": "Status: closed prevents update"}]}},
            status=422)
    async with connect() as c:
        r = await c.call_tool("update_ticket", {"ticket_id": 9, "status": "open"})
    assert r.is_error
    assert "HTTP 422" in text(r) and "closed prevents update" in text(r)


async def test_rate_limit_retry(connect, fake):
    state = {"n": 0}

    def flaky(req):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate limited"})
        return {"user": {"id": 1, "name": "Me"}}

    fake.on("GET", "/api/v2/users/me", flaky)
    async with connect() as c:
        out = data(await c.call_tool("get_user", {"user_id": "me", "include_organizations": False}))
    assert out["user"]["name"] == "Me" and state["n"] == 2


# ---------------------------------------------------------------- resources
async def test_resource_crud_wrapping(connect, fake):
    fake.on("POST", "/api/v2/macros", lambda r: {"macro": {"id": 1, **json.loads(r.content)["macro"]}})
    fake.on("POST", "/api/v2/help_center/categories/5/sections", {"section": {"id": 9}})
    fake.on("PATCH", "/api/v2/webhooks/w1", {"ok": True})
    async with connect() as c:
        out = data(await c.call_tool("create_resource", {"resource": "macros", "data": {"title": "Close it"}}))
        assert out["macro"]["title"] == "Close it" and fake.body() == {"macro": {"title": "Close it"}}

        r = await c.call_tool("create_resource", {"resource": "help_center_sections", "data": {"name": "FAQ"}})
        assert r.is_error and "parent_id" in text(r)
        data(await c.call_tool("create_resource", {"resource": "help_center_sections", "data": {"name": "FAQ"},
                                                   "parent_id": 5}))
        data(await c.call_tool("update_resource", {"resource": "webhooks", "id": "w1", "data": {"name": "n"}}))

        r = await c.call_tool("delete_resource", {"resource": "satisfaction_ratings", "id": 1})
        assert r.is_error and "read-only" in text(r)


# ---------------------------------------------------------------- help center
async def test_create_article_defaults_permission_group(connect, fake):
    fake.on("GET", "/api/v2/guide/permission_groups", {"permission_groups": [{"id": 77, "name": "Admins"}],
                                                       "meta": {"has_more": False}})
    fake.on("POST", "/api/v2/help_center/sections/3/articles",
            lambda r: {"article": {"id": 1, "body": "<p>x</p>", **json.loads(r.content)["article"]}})
    async with connect() as c:
        out = data(await c.call_tool("create_article", {"section_id": 3, "title": "T", "body": "<p>x</p>"}))
    sent = fake.body()["article"]
    assert sent["permission_group_id"] == 77 and sent["user_segment_id"] is None and sent["draft"] is True
    assert "body" not in out["article"] and "Admins" in out["note"]


# ---------------------------------------------------------------- export & attachments
async def test_incremental_export_iso_start(connect, fake):
    fake.on("GET", "/api/v2/incremental/tickets/cursor",
            {"tickets": [{"id": 1}], "after_cursor": "c2", "end_of_stream": False})
    async with connect() as c:
        out = data(await c.call_tool("incremental_export", {"resource": "tickets", "start_time": "2026-10-01"}))
    assert fake.calls[-1].url.params["start_time"] == "1790812800"
    assert out["after_cursor"] == "c2" and out["end_of_stream"] is False


async def test_upload_text_attachment(connect, fake):
    fake.on("POST", "/api/v2/uploads", {"upload": {"token": "T1", "attachment": {"id": 5, "file_name": "log.txt"}}},
            status=201)
    async with connect() as c:
        out = data(await c.call_tool("upload_attachment", {"filename": "log.txt", "text": "hello"}))
        r = await c.call_tool("upload_attachment", {"filename": "x", "file_path": "/etc/passwd"})
    assert out["token"] == "T1"
    req = [x for x in fake.calls if x.url.path == "/api/v2/uploads"][0]
    assert req.content == b"hello" and req.headers["content-type"] == "text/plain"
    assert r.is_error and "ZENDESK_FILE_ROOT" in text(r)


async def test_file_root_confinement(connect, fake, tmp_path):
    fake.on("POST", "/api/v2/uploads", {"upload": {"token": "T"}})
    (tmp_path / "ok.txt").write_text("fine")
    async with connect(file_root=str(tmp_path)) as c:
        assert data(await c.call_tool("upload_attachment", {"filename": "ok.txt", "file_path": "ok.txt"}))["token"] == "T"
        r = await c.call_tool("upload_attachment", {"filename": "x", "file_path": "../../etc/passwd"})
    assert r.is_error and "inside ZENDESK_FILE_ROOT" in text(r)


async def test_download_image_returned_and_no_auth_off_host(connect, fake):
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    fake.on("GET", "/api/v2/attachments/99", {"attachment": {
        "id": 99, "file_name": "s.png", "content_type": "image/png", "size": len(png),
        "content_url": "https://acme.zdusercontent.com/attachment/123/s.png"}})
    fake.on("GET", "/attachment/123/s.png", httpx.Response(200, content=png, headers={"Content-Type": "image/png"}))
    async with connect() as c:
        r = await c.call_tool("download_attachment", {"attachment_id": 99})
    assert not r.is_error
    kinds = [b.type for b in r.content]
    assert kinds == ["text", "image"] and r.content[1].mime_type == "image/png"
    cdn = fake.calls[-1]
    assert cdn.url.host == "acme.zdusercontent.com" and "authorization" not in cdn.headers


# ---------------------------------------------------------------- formatting
def test_render_trims_lists_but_stays_valid_json():
    payload = {"tickets": [{"id": i, "subject": "x" * 200} for i in range(500)], "has_more": True}
    out = render(payload, 10_000)
    parsed = json.loads(out)
    assert len(out) <= 10_000
    assert 0 < len(parsed["tickets"]) < 500 and parsed["_truncated"]
