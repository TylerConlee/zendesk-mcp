"""Help Center (Guide) article tools."""

from typing import Annotated, Any

from pydantic import Field

from ..client import ZendeskError
from ..toolkit import Toolkit


def _slim_article(a: dict[str, Any], include_body: bool) -> dict[str, Any]:
    a = dict(a)
    if not include_body:
        body = a.pop("body", None) or ""
        a["body_chars"] = len(body)
    a.pop("url", None)
    return a


def register(tk: Toolkit) -> None:
    zd = tk.zd

    @tk.tool("read", title="Search Help Center")
    async def search_articles(
        query: str,
        locale: Annotated[str | None, Field(description="e.g. 'en-us'. Omit for all locales.")] = None,
        section_id: int | None = None,
        category_id: int | None = None,
        label_names: Annotated[list[str] | None, Field(description="Only articles with all of these labels.")] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 10,
        include_body: bool = False,
    ) -> str:
        """Full-text search of Help Center articles. Returns snippets; use get_article for full bodies."""
        params = {
            "query": query, "locale": locale, "section": section_id, "category": category_id,
            "label_names": ",".join(label_names) if label_names else None,
        }
        page = await zd.paginate("help_center/articles/search", "results", params=params, limit=limit, cursor=False)
        page["results"] = [_slim_article(a, include_body) for a in page["results"]]
        return tk.out(page)

    @tk.tool("read", title="Get article")
    async def get_article(
        article_id: int,
        locale: Annotated[str | None, Field(description="Translation locale; omit for the default.")] = None,
        include_translations_list: bool = False,
    ) -> str:
        """Get a Help Center article including its HTML body."""
        path = f"help_center/{locale}/articles/{article_id}" if locale else f"help_center/articles/{article_id}"
        data = await zd.get(path)
        if include_translations_list:
            tr = await zd.paginate(f"help_center/articles/{article_id}/translations", "translations", limit=100,
                                   cursor=False)
            data["translations"] = [{"locale": t.get("locale"), "title": t.get("title"), "draft": t.get("draft"),
                                     "outdated": t.get("outdated"), "updated_at": t.get("updated_at")}
                                    for t in tr["translations"]]
        return tk.out(data)

    @tk.tool("write", title="Create article")
    async def create_article(
        section_id: int,
        title: str,
        body: Annotated[str, Field(description="Article body as HTML.")],
        locale: str = "en-us",
        draft: Annotated[bool, Field(description="Create as an unpublished draft.")] = True,
        user_segment_id: Annotated[int | None, Field(description="Who can view it; null = everyone.")] = None,
        permission_group_id: Annotated[int | None, Field(description="Who can edit it; defaults to the first permission group.")] = None,
        label_names: list[str] | None = None,
        promoted: bool = False,
        notify_subscribers: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """Create a Help Center article (as a draft by default)."""
        note = None
        if permission_group_id is None:
            groups = await zd.paginate("guide/permission_groups", "permission_groups", limit=1)
            if not groups["permission_groups"]:
                raise ZendeskError("No permission groups found; pass permission_group_id.")
            permission_group_id = groups["permission_groups"][0]["id"]
            note = f"Used permission group {groups['permission_groups'][0].get('name')!r} ({permission_group_id})."
        article: dict[str, Any] = {
            "title": title, "body": body, "locale": locale, "draft": draft, "user_segment_id": user_segment_id,
            "permission_group_id": permission_group_id, "promoted": promoted,
        }
        if label_names:
            article["label_names"] = label_names
        article.update(extra or {})
        data = await zd.request("POST", f"help_center/sections/{section_id}/articles",
                                json_body={"article": article, "notify_subscribers": notify_subscribers})
        result = {"article": _slim_article(data["article"], include_body=False)}
        if note:
            result["note"] = note
        return tk.out(result)

    @tk.tool("write", title="Update article", idempotent=True)
    async def update_article(
        article_id: int,
        title: str | None = None,
        body: Annotated[str | None, Field(description="New HTML body (replaces the translation body).")] = None,
        draft: bool | None = None,
        locale: Annotated[str, Field(description="Translation to update for title/body/draft.")] = "en-us",
        section_id: Annotated[int | None, Field(description="Move to another section.")] = None,
        label_names: list[str] | None = None,
        promoted: bool | None = None,
        user_segment_id: int | None = None,
        permission_group_id: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """Update an article. Title/body/draft are per-translation; section/labels/permissions are article-level."""
        results: dict[str, Any] = {}
        translation = {k: v for k, v in {"title": title, "body": body, "draft": draft}.items() if v is not None}
        if translation:
            tr = await zd.request("PUT", f"help_center/articles/{article_id}/translations/{locale}",
                                  json_body={"translation": translation})
            t = tr.get("translation", {})
            results["translation"] = {k: t.get(k) for k in ("locale", "title", "draft", "updated_at")}
        meta = {k: v for k, v in {
            "section_id": section_id, "label_names": label_names, "promoted": promoted,
            "user_segment_id": user_segment_id, "permission_group_id": permission_group_id,
        }.items() if v is not None}
        meta.update(extra or {})
        if meta:
            a = await zd.request("PUT", f"help_center/articles/{article_id}", json_body={"article": meta})
            results["article"] = _slim_article(a["article"], include_body=False)
        if not results:
            raise ZendeskError("Nothing to update.")
        return tk.out(results)
