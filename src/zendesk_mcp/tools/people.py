"""User and organization tools beyond plain CRUD (which lives in resources.py)."""

from typing import Annotated, Any

from pydantic import Field

from ..client import ZendeskError
from ..formatting import project
from ..toolkit import Toolkit


def register(tk: Toolkit) -> None:
    zd = tk.zd

    @tk.tool("read", title="Get user")
    async def get_user(
        user_id: Annotated[int | str, Field(description="User ID, or 'me' for the authenticated API user.")],
        include_identities: Annotated[bool, Field(description="Include all emails/phones/social identities.")] = False,
        include_organizations: bool = True,
    ) -> str:
        """Get a user profile (with organization names, and optionally identities)."""
        params = {"include": "organizations"} if include_organizations else {}
        data = await zd.request("GET", f"users/{user_id}", params=params)
        result: dict[str, Any] = {"user": data["user"]}
        if data.get("organizations"):
            result["organizations"] = [{"id": o["id"], "name": o.get("name")} for o in data["organizations"]]
        if include_identities:
            uid = data["user"]["id"]
            result["identities"] = (await zd.paginate(f"users/{uid}/identities", "identities", limit=100))["identities"]
        return tk.out(result)

    @tk.tool("read", title="Search users")
    async def search_users(
        query: Annotated[str | None, Field(description="Name, email, phone or search terms (e.g. 'jane@acme.com', 'role:agent').")] = None,
        external_id: str | None = None,
        limit: Annotated[int, Field(ge=1, le=1000)] = 25,
        fields: list[str] | None = None,
    ) -> str:
        """Find users by email, name, phone, external ID or search query."""
        if not query and not external_id:
            raise ZendeskError("Provide `query` or `external_id`.")
        params = {"external_id": external_id} if external_id else {"query": query}
        page = await zd.paginate("users/search", "users", params=params, limit=limit, cursor=False)
        page["users"] = project(page["users"], fields)
        return tk.out(page)

    @tk.tool("write", title="Create or update user", idempotent=True)
    async def create_or_update_user(
        user: Annotated[dict[str, Any], Field(description="User attributes. Matched on email or external_id; "
                                              "e.g. {'name':'Jane','email':'jane@acme.com','role':'end-user',"
                                              "'organization_id':123,'user_fields':{'plan':'pro'}}")],
    ) -> str:
        """Upsert a user: updates the existing user with a matching email/external_id, otherwise creates one."""
        return tk.out(await zd.request("POST", "users/create_or_update", json_body={"user": user}))

    @tk.tool("write", title="Merge users")
    async def merge_users(
        source_user_id: Annotated[int, Field(description="User that will be merged away (ceases to exist).")],
        target_user_id: Annotated[int, Field(description="User that remains.")],
    ) -> str:
        """Merge one end user into another. Tickets, identities and memberships move to the target. Irreversible."""
        return tk.out(await zd.request("PUT", f"users/{source_user_id}/merge", json_body={"user": {"id": target_user_id}}))

    @tk.tool("read", title="Search organizations")
    async def search_organizations(
        name: Annotated[str | None, Field(description="Prefix match on organization name (autocomplete).")] = None,
        external_id: str | None = None,
        limit: Annotated[int, Field(ge=1, le=1000)] = 25,
    ) -> str:
        """Find organizations by name prefix or external ID. For richer filters use
        search(query='type:organization ...')."""
        if external_id:
            page = await zd.paginate("organizations/search", "organizations", params={"external_id": external_id},
                                     limit=limit, cursor=False)
        elif name:
            page = await zd.paginate("organizations/autocomplete", "organizations", params={"name": name},
                                     limit=limit, cursor=False)
        else:
            raise ZendeskError("Provide `name` or `external_id`.")
        return tk.out(page)
