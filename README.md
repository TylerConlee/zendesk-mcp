# zendesk-mcp

A self-hosted [Model Context Protocol](https://modelcontextprotocol.io) server that gives an LLM full access to a Zendesk instance — Support, Help Center, and anything else under `/api/v2` — using your own API credentials. No third-party relay, no usage restrictions beyond the access mode you choose.

- **72 tools**: curated tools for the common work (tickets, search, users, articles, macros), support analytics, presentation-ready reporting, customer and account insights, follow-through and operations reviews, full administration (agents, rules, settings, configuration-as-code rollouts), generic CRUD over 52 resource types, and a raw `api_request` escape hatch for every other endpoint.
- **Safe administration**: every write is journaled with the object's prior state and can be reverted; configuration rollouts are planned (a reviewable diff) before they're applied; configuration can be snapshotted, diffed and documented in plain language.
- **Deck-ready reporting**: one call (`support_report`) returns a scorecard vs last period and vs your targets, trends, contact drivers, customer quotes, SLA, a staffing heatmap, biggest issues, backlog and Help Center gaps — redacted and team-level by default for company-wide audiences.
- **Support metrics built in**: KPIs from Zendesk's own ticket metrics, plus the time between *every* customer message and agent reply (not just the first), in calendar or business hours from your Zendesk schedules.
- **Built for LLMs**: responses are slimmed (null fields, duplicate `fields` arrays and empty custom fields removed), user/group/org IDs are resolved to names, large results are trimmed by whole items so JSON stays valid, and API errors come back with Zendesk's validation details.
- **Safe to run**: three access modes enforced at the HTTP layer, MCP tool annotations (read-only / destructive hints) so clients can ask before risky calls, credentials never sent to other hosts, file access confined to one directory.
- **Two transports**: stdio for Claude Desktop / Claude Code, streamable HTTP with bearer auth for a home server, Docker, or an aggregator like MetaMCP.
- Automatic retries on rate limits (`Retry-After`) and transient errors; cursor and offset pagination handled for you.

## Tools

| Area | Tools |
|---|---|
| Search | `search` (full Zendesk query syntax, sorting, export mode for >1000 results, count-only) |
| Tickets | `get_ticket`, `list_tickets`, `create_ticket`, `update_ticket`, `add_ticket_comment`, `list_ticket_comments`, `list_ticket_audits`, `bulk_update_tickets`, `merge_tickets`, `delete_tickets`, `apply_macro`, `manage_tags` |
| People | `get_user`, `search_users`, `create_or_update_user`, `merge_users`, `search_organizations` |
| Everything configurable | `list_resources`, `get_resource`, `create_resource`, `update_resource`, `delete_resource` |
| Help Center | `search_articles`, `get_article`, `create_article`, `update_article` |
| Analytics | `support_kpis`, `reply_time_report`, `ticket_reply_times`, `backlog_report`, `sla_report` |
| Reporting | `support_report`, `kpi_trends`, `ticket_topics`, `sample_ticket_text`, `csat_feedback`, `volume_heatmap`, `top_issues`, `help_center_report`, `report_targets` |
| Customers & accounts | `customer_profile`, `account_health`, `customer_timeline` |
| Follow-through | `promise_tracker`, `ticket_brief` |
| Patterns | `emerging_issues`, `deflection_finder` |
| Operations | `routing_report`, `macro_report`, `agent_profile`, `capacity_forecast` |
| Administration | `list_agents`, `provision_agents`, `offboard_agent`, `rule_definitions`, `preview_view`, `reorder`, `find_references`, `account_settings`, `document_config` |
| Rollouts & safety | `blueprint_export`, `blueprint_plan`, `blueprint_apply`, `config_snapshot`, `snapshot_diff`, `list_changes`, `revert_change` |
| Advanced | `api_request`, `incremental_export`, `upload_attachment`, `download_attachment` |

The resource tools cover: users, organizations, groups, group/organization memberships, user identities, custom roles, routing skills and values, omnichannel queues, group SLA policies, user field options, ticket fields (+ options), ticket forms, custom statuses, user/organization fields, brands, tags, macros, triggers, trigger categories, automations, views, SLA policies, schedules (+ holidays), dynamic content, targets, webhooks, recipient addresses, sharing agreements, custom objects (+ records), suspended/deleted tickets, ticket metrics, satisfaction ratings/reasons, side conversations, job statuses, audit logs, activities, locales, and Help Center categories, sections, articles, community topics/posts, user segments and permission groups.

`download_attachment` returns images as images, so the model can actually look at customer screenshots.

## Metrics and reply times

All durations are in minutes. Pass `business_hours: true` to any reply-time tool to also get business-hours figures, using your Zendesk schedule and its holidays (DST-aware).

| Tool | What it answers |
|---|---|
| `support_kpis` | For a date range (by created, solved or updated date, plus any search filter): volume by status/channel/priority, first reply time, first and full resolution time, requester wait, agent wait, on-hold time (each as n/mean/median/p90/min/max, calendar and business), one-touch and zero-touch resolution %, reopen %, average reassignments, CSAT score and response rate. Optional breakdown by assignee, group, channel, priority, type, brand, form, organization or tag. |
| `reply_time_report` | Time between customer and agent replies across many tickets, computed from the actual comments: agent response time for every customer message, first response, customer response time, per-agent and per-group median/p90, the 10 slowest responses, and tickets waiting on an agent right now. With a date range, only replies inside the range count. |
| `ticket_reply_times` | The full back-and-forth on one ticket, each gap listed with the agent's name, alongside Zendesk's own metrics for comparison. |
| `backlog_report` | Unsolved tickets right now: by status, age buckets, stale (no update in N days), unassigned, a breakdown, and the oldest tickets. |
| `sla_report` | SLA achievement % by policy and metric (first reply, next reply, requester wait, agent work time…) from ticket metric events, plus targets currently in breach. |

How reply gaps are measured:

- **Agent response time** runs from the *first* unanswered customer message to the next agent reply. If the customer sends three messages in a row, the clock started at the first.
- **Customer response time** runs from the agent's *last* message to the customer's next message.
- Internal notes and trigger/automation comments don't count as agent replies by default (`include_internal_notes`, `include_automated` turn them on). Roles come from each comment's author (end-user vs agent/admin).

Zendesk's own "first reply time" and "requester wait time" (used by `support_kpis`) are computed by Zendesk itself and won't always match these conversation-based numbers exactly — e.g. Zendesk counts from ticket creation and uses the schedule attached to each ticket. `reply_time_report` uses one schedule for all tickets (the first one, or `schedule_id`). `sla_report` approximates Explore's achievement definition: achieved = fulfilled in range with no breach, breached = breached (including fulfilled late).

Cost: `support_kpis` makes roughly one API call per 100 tickets; `reply_time_report` makes one call per ticket (5 at a time, rate limits handled), so keep `max_tickets` modest for big ranges.

## Building a presentation

Ask for the deck and let Claude call `support_report`:

> "Build a Q3 support review deck for the company from Zendesk. Use business hours."

`support_report` gathers everything in one call (default period: last quarter, compared with the quarter before):

| Section | What's in it |
|---|---|
| `headline` | Scorecard: tickets, solved, first reply and resolution medians, customer wait, one-touch %, reopen %, CSAT, SLA achievement, backlog. Each has current, previous, change %, improved/worsened, and green/amber/red if you've set a target. |
| `trends` | One row per week (or day/month) for charts. |
| `channels` | Channel mix now vs previous period. |
| `topics` | Contact drivers from your dropdown fields and tags: volume, share, growth, CSAT and speed per topic, rising and falling topics. |
| `csat` | Good/bad totals, reasons for bad ratings, CSAT by group, redacted customer quotes. |
| `sla` | Achievement by policy and metric, vs previous period. |
| `heatmap` | Day × hour volume, peaks, and first reply time by hour of arrival — the staffing slide. |
| `issues` | Problems with the most linked incidents, top and lowest-CSAT customers, highest-effort and most-reopened tickets. |
| `backlog` | Unsolved now, by status, age and group. |
| `help_center` | Most/least helpful articles, stale articles, and top ticket topics with no matching article. |
| `definitions`, `suggested_slides`, `data_notes` | Appendix text, a slide outline mapped to the data, and caveats to footnote. |

Drill-down tools for individual slides: `kpi_trends` (any metric over time, split by channel/group/etc.), `ticket_topics`, `sample_ticket_text` (a reproducible random sample of ticket text so Claude can find free-text themes and estimate their volume), `csat_feedback`, `volume_heatmap`, `top_issues`, `help_center_report`.

**Periods** accept `last_quarter`, `this_quarter`, `last_month`, `ytd`, `last_30_days`, `2026-Q3`, `2026-09`, `2026`, or explicit `start`/`end`. "This quarter" is compared with the same number of days into last quarter, so partial periods compare fairly.

**Targets** turn the scorecard green/amber/red. Set them once by asking Claude ("set our CSAT target to 92% and first reply to 60 business minutes") — `report_targets` stores them in `zendesk_targets.json` (in `ZENDESK_FILE_ROOT`, or `~/.zendesk-mcp/`, or `ZENDESK_TARGETS_FILE`). Amber means within 10% of the goal on the wrong side (configurable per metric).

**Privacy** defaults suit an all-hands: `team_level=true` hides per-agent figures and agent names, and `redact=true` strips customer and agent names, emails, links and long numbers (phones, card/account numbers) from quotes, subjects and ticket text. `anonymize_orgs=true` shows customers as "Customer 1, 2…". Turn these off for internal team reviews. Redaction is pattern- and name-based, so skim quotes before presenting.

**Charts and spreadsheets**: chart data comes back as tidy rows. Add `csv: "inline"` to get CSV text, or `csv: "files"` to write CSVs under `ZENDESK_FILE_ROOT/reports/`.

**Cost and speed**: about one API call per 100 tickets per period, plus a handful for SLA, CSAT and Help Center — ~250 calls for a quarter with 6,000 tickets (compared against the previous quarter), so one to two minutes on most plans' rate limits. Results are cached for 10 minutes, so follow-up drill-downs on the same period are fast. `reply_gap_sample: N` adds conversation-level reply gaps for N sampled tickets at one call each.

## Customer profiles

Ask about one customer and let Claude call `customer_profile`:

> "Give me a profile of Acme Corp for their QBR — last quarter, and how they compare to everyone else."

Pass an organization name (exact, prefix or keywords) or ID. If a name matches several organizations, the tool lists them with IDs instead of guessing. Default period: last 90 days, compared with the 90 days before.

| Section | What's in it |
|---|---|
| `customer` | Account facts: customer since, tags, domains, custom organization fields (labelled), users and all-time ticket count. |
| `signals` | Risks, watch-outs and positives worth leading with — volume spikes, CSAT drops, slower replies than other customers, reopens, open urgent tickets, stale backlog, SLA misses, frustration in conversations. |
| `volume` | Tickets vs the previous period, share of all tickets, weekly/monthly trend, channel/priority/type/status mix, contact drivers (rising and falling topics), top contacts. |
| `performance` | Scorecard — first reply, resolution, customer wait, one-touch, reopens, CSAT — vs the previous period and vs all customers (`better`/`worse`), plus SLA achievement on their tickets. |
| `backlog_now` | Their unsolved tickets right now: by status, age, stale, unassigned, oldest. |
| `challenging_tickets` | Their hardest tickets, ranked by replies, reopens, transfers, bad CSAT, priority and time open, each with the reasons in words and a link. |
| `sentiment` | CSAT score, bad-rating reasons and comments; any AI sentiment field or tags already in Zendesk; and the tone of their own messages in a sample of conversations (frustration, chasing and churn language vs thanks and praise), with excerpts and the reply pace both ways. |

Conversation tone is a transparent keyword heuristic, not a model — excerpts come back so Claude can read the actual words before describing the relationship. `conversations` controls how many tickets are read (one API call each; the hardest tickets first). `benchmark=false` skips the all-customer comparison on very large accounts. Like the reports, text is redacted and agent names left out by default; set `redact=false` to see contact names and `team_level=false` for assignees.

## Insight tools

Each answers one question in one call, returns the evidence behind its answer (ticket IDs, excerpts, reasons), and follows the same privacy defaults as the reports.

| Tool | Question it answers | What comes back |
|---|---|---|
| `account_health` | Which customers should I worry about? | Every organization scored 0–100 (healthy / watch / at risk / quiet) with the reasons — CSAT, bad ratings, open urgent tickets, idle tickets, volume spikes, reopens, slow first replies, negative AI sentiment — plus renewal date, plan, ARR and CSM-style org fields. Filter with `renewing_within_days`, `org_tags` or `field_filters`. |
| `customer_timeline` | What's happened with this customer? | Notable tickets opened and solved, ratings with comments, reopens, problem links, quiet stretches and what's still open, in date order, with a month-by-month rollup. |
| `promise_tracker` | What did we say we'd do, and did we? | Commitments in agent replies ("I'll follow up tomorrow", "fix by Friday") with deadlines read from the wording, marked kept, late, overdue or pending — plus customers waiting on us, longest first. |
| `ticket_brief` | What's going on with this ticket? | A headline, who it's waiting on, a merged timeline of messages, notes, field changes and macros (with the trigger or agent behind each), customer waits, tone trend, promises and related tickets. Ready for a Slack escalation. |
| `emerging_issues` | Is something breaking for several customers? | Words, phrases, dropdown values and tags running well above their usual rate in the last few days across several customers, grouped into issues with customers affected, daily counts and examples. |
| `deflection_finder` | What could we stop answering by hand? | Groups of similar solved tickets ranked by agent effort, whether agents give the same answer each time, existing Help Center and macro coverage, a suggested fix, and the typical reply as a draft. |
| `routing_report` | Are tickets landing in the right place? | First group vs solving group, transfer paths, ping-pong, which triggers or agents move tickets, by form and channel, and what transfers cost in reply time, resolution and CSAT. |
| `macro_report` | Which macros help? | Most used, outcomes on tickets where each macro was used vs none, and unused, stale and duplicate macros. |
| `agent_profile` | How is this agent doing? | A scorecard vs their last period and the team, strengths and focus areas, hardest tickets, CSAT comments, open queue, and samples of their replies. For managers, not for sharing widely. |
| `capacity_forecast` | Will we keep up? | Weekly demand forecast vs solve capacity, the projected backlog week by week, solves per agent and the busiest day/hour slots. |

Text-based signals (tone, promises, similar tickets) are transparent heuristics — keyword and phrase matching, word-overlap similarity — not a language model. Each tool says how it works in its output and returns excerpts so Claude can check them.

## Administering Zendesk

Everything an admin does in Admin Center can be done through Claude, with guardrails.

### Day-to-day admin

- **Agents**: `list_agents` gives the roster (roles, custom roles, groups, last login, inactive agents). `provision_agents` creates or updates up to 50 agents in one call (role, custom role, groups, default group), matching existing users by email; use `dry_run` to preview. `offboard_agent` follows Zendesk's recommended order (reassign unsolved tickets → remove memberships → downgrade/suspend/delete) and **defaults to a dry run** that shows the impact.
- **Building rules**: `rule_definitions` lists the valid conditions and actions (with your group, field and status values) so triggers, automations, views and macros are built correctly. `preview_view` shows how many tickets a view's conditions match before you save it. `reorder` changes trigger, automation, view, macro, form or SLA order.
- **Before changing anything**: `find_references` lists every trigger, automation, view, macro, SLA policy, form and queue that uses a group, field, status, form, brand, schedule, webhook, user, organization or tag.
- **Settings**: `account_settings` reads and updates account-level settings.
- **Everything else** (create/update/delete any config object) goes through `create_resource` / `update_resource` / `delete_resource`, or `api_request`. `update_resource` protects against Zendesk's replace-everything updates: changing only a trigger's or automation's conditions keeps its actions (and vice versa), changing a view's `all` conditions keeps its `any` ones, and an update that would drop dropdown options is refused unless you pass `allow_option_removal` (dropped options lose their values on every ticket and macro).
- `provision_agents` and blueprint agents only change a user's role when you specify one, so listing an existing admin to add a group never demotes them.

### Documenting the configuration

`document_config` writes a Markdown (and JSON) document describing what everything does in plain language: every trigger in firing order ("When Ticket is Created and Priority is Urgent → Group → “Tier 2”, Notify webhook “PagerDuty”"), automations, views and who sees them, macros, SLA targets, forms and their conditional logic, fields and options, statuses, business hours, webhooks, routing and key settings. IDs are resolved to names using Zendesk's own labels. It also shows where each group/field/status/form/webhook is used and a **findings** list: rules pointing at deleted objects, active triggers calling inactive webhooks, unused fields and groups, rules not used in 30 days, duplicate names. Agent names are left out unless `include_agents=true`, so the document is safe to share. Files land in `ZENDESK_FILE_ROOT/docs/`.

### Rolling out a configuration (e.g. an architecture plan)

1. **Snapshot** — `config_snapshot(label="pre-rollout")` saves the entire configuration to `ZENDESK_FILE_ROOT/snapshots/`.
2. **Describe the target** — a *blueprint* lists the configuration you want, by name. `blueprint_export` writes the current configuration in the same format, so you can see the starting point or edit it. References between objects use names, so new objects can refer to each other before they exist:

   ```yaml
   groups:
     - name: Tier 2
       description: Escalations
   ticket_fields:
     - title: Severity
       type: tagger
       custom_field_options:
         - {name: Sev 1, value: sev_1}
         - {name: Sev 2, value: sev_2}
   trigger_categories:
     - name: Routing
   triggers:
     - title: Route Sev 1 to Tier 2
       category: Routing
       conditions:
         all:
           - {field: "@custom_field:Severity", operator: is, value: "@option:Severity/Sev 1"}
       actions:
         - {field: group_id, value: "@group:Tier 2"}
   agents:
     - {email: sam@acme.com, name: Sam, groups: [Tier 2], custom_role: Light Agent}
   order:
     triggers: [Route Sev 1 to Tier 2, Notify requester of received request]
   delete:
     macros: [Old escalation macro]
   ```

   Sections: groups, custom_roles, brands, schedules (with `intervals` and `holidays`), trigger_categories, custom_statuses, user_fields, organization_fields, ticket_fields, webhooks, skills, ticket_forms, agents, sla_policies, triggers, automations, views, macros, plus `settings`, `order` and `delete`. References: `@group:`, `@role:`, `@brand:`, `@schedule:`, `@category:`, `@status:`, `@field:` (→ id), `@custom_field:` (→ `custom_fields_<id>` in rules), `@option:Field/Option` (→ tag), `@user_field:`, `@org_field:`, `@form:`, `@webhook:`, `@sla:`, `@trigger:`/`@automation:`/`@view:`/`@macro:`, `@skill:Attribute/Value`, `@user:email`, `@org:Name`, `@target:`.
3. **Plan** — `blueprint_plan` compares the blueprint with the live account and lists every create, update (before → after), reorder and delete. It reports blocking errors (unknown references, ambiguous names, missing required fields, automations without a time condition, triggers Zendesk says are invalid) and warnings (e.g. deleting a group that rules still use). Nothing changes.
4. **Apply** — after you've reviewed the plan, `blueprint_apply(plan_id)` re-plans first and **refuses if anything changed since review** (showing the new plan instead), then applies in dependency order: groups/fields/statuses before forms, agents and rules; deletes last.
5. **Verify or undo** — `snapshot_diff(before="<snapshot>")` shows exactly what changed. `revert_change(batch="<plan id>")` rolls back the whole rollout.

Blueprint rules that keep it safe:

- **Only what you write is managed.** Keys you leave out of an object are left alone. Applying the same blueprint twice does nothing the second time.
- **Nothing is deleted unless it's listed under `delete`.**
- **Dropdown options are merged, not replaced.** Zendesk removes an option's value from every ticket and macro when the option is dropped, so existing options you don't list are kept unless you set `prune_options: true` on that field (the plan warns when it would remove any).
- **Triggers and automations always get both their conditions and actions sent**, because Zendesk clears both when either is updated.
- Webhook credentials are never compared or re-sent on update unless you set `rotate_authentication: true`, and they're redacted in plans and the journal.

### The change journal and undo

Every write the server makes — from any tool, including `api_request` — is appended to `zendesk_changes.jsonl` (in `ZENDESK_FILE_ROOT`, otherwise `~/.zendesk-mcp/`), with the request (secrets redacted), the outcome, and the object's prior state where a tool captured it. `list_changes` shows them; `revert_change` undoes one change or a whole batch:

- **Updates** are rolled back to the previous values of the keys that changed. A revert refuses if the object was changed again since, unless you pass `force`.
- **Creates** are undone by deleting the object — only when Zendesk actually created something (a create-or-update call that modified an existing object is never "undone" by deleting it).
- **Deletes** are undone by re-creating the object. It gets a new ID, so rules that referred to the old ID need updating (`find_references` helps).
- **Reorders and settings** are restored.
- **Bulk ticket updates can't be undone** (Zendesk's own ticket audits keep that history).

### Use a sandbox first

If your plan includes Zendesk's sandbox, point a second copy of the server at it and rehearse there:

```bash
cp .env .env.sandbox   # then set ZENDESK_SUBDOMAIN (and credentials) to the sandbox
docker compose -f docker-compose.sandbox.yml up -d --build
claude mcp add --transport http --scope user zendesk-sandbox http://localhost:8001/mcp \
  --header "Authorization: Bearer $(sed -n 's/^MCP_AUTH_TOKEN=//p' .env.sandbox | xargs)"
```

A blueprint applied to the sandbox can be applied unchanged to production: references are by name, not ID.

### Keeping the tool list focused

Set `ZENDESK_TOOLSETS` to load only what a session needs: `core` (tickets, search, users, resources, Help Center, raw API, journal), `analytics` (metrics and reporting), `admin` (administration, blueprints, documentation, journal). Combine with commas; default `all`. For example, run an admin-only server for the rollout and a `core,analytics` one for day-to-day work.

## Setup

**1. Get credentials** — either:

- **API token**: Admin Center → Apps and integrations → APIs → Zendesk API, enable token access and create a token. Set `ZENDESK_EMAIL` + `ZENDESK_API_TOKEN`. The server acts with that user's permissions (an admin gives full access).
- **OAuth client**: Admin Center → Apps and integrations → APIs → OAuth clients → create a client with kind **Confidential**. Set `ZENDESK_CLIENT_ID` (the client's *Identifier*) + `ZENDESK_CLIENT_SECRET`. The server uses Zendesk's client-credentials grant to get access tokens and renews them automatically (they expire after at most 48 hours and come without refresh tokens). Tokens act as the user associated with the client. Scope defaults to `read write` (`read` in `read_only` mode); override with `ZENDESK_OAUTH_SCOPE`.

A client secret is not an access token — don't put it in `ZENDESK_OAUTH_TOKEN`, which is only for an access token you already have.

**2. Install** (Python 3.10+):

```bash
cd zendesk-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env    # then fill in ZENDESK_SUBDOMAIN, ZENDESK_EMAIL, ZENDESK_API_TOKEN
```

**3. Connect a client.**

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "zendesk": {
      "command": "/absolute/path/to/zendesk-mcp/.venv/bin/zendesk-mcp",
      "env": {
        "ZENDESK_SUBDOMAIN": "yourcompany",
        "ZENDESK_EMAIL": "you@yourcompany.com",
        "ZENDESK_API_TOKEN": "…",
        "ZENDESK_ACCESS_MODE": "full"
      }
    }
  }
}
```

Claude Code:

```bash
claude mcp add zendesk \
  --env ZENDESK_SUBDOMAIN=yourcompany \
  --env ZENDESK_EMAIL=you@yourcompany.com \
  --env ZENDESK_API_TOKEN=… \
  -- /absolute/path/to/zendesk-mcp/.venv/bin/zendesk-mcp
```

(If you run the binary from the project directory, it also reads `.env` there.)

## Running as an HTTP server (Docker / home server)

```bash
cp .env.example .env               # fill in Zendesk creds
echo "MCP_AUTH_TOKEN=$(openssl rand -hex 32)" >> .env
mkdir -p data && chmod a+rwX data  # the container's non-root user writes the journal, snapshots and docs here
docker compose up -d --build
curl http://localhost:8000/health  # {"status":"ok"}
```

The MCP endpoint is `http://<host>:8000/mcp` and every request needs `Authorization: Bearer <MCP_AUTH_TOKEN>`. The server refuses to listen on a non-localhost interface without a token, since that token is effectively your Zendesk admin key.

Point clients at it, e.g. Claude Code:

```bash
claude mcp add --transport http zendesk http://homeserver:8000/mcp \
  --header "Authorization: Bearer <MCP_AUTH_TOKEN>"
```

or add it to MetaMCP as a Streamable HTTP server with that bearer header. It runs stateless with JSON responses, so it works fine behind reverse proxies and aggregators.

Without Docker: `zendesk-mcp --transport http --host 0.0.0.0 --port 8000`.

If you expose it beyond your LAN, put it behind TLS (Caddy, Traefik, Cloudflare Tunnel) and set `MCP_ALLOWED_HOSTS` to your hostname.

## Access modes

Set `ZENDESK_ACCESS_MODE`:

| Mode | Behaviour |
|---|---|
| `full` (default) | Everything, including deletes. |
| `no_delete` | Blocks every HTTP `DELETE` (removing tags still works). Delete tools are hidden. |
| `read_only` | Blocks everything except `GET`. Write tools are hidden; `search`, reads and `api_request` GETs still work. |

The mode is enforced in the HTTP client, so it also applies to `api_request` — the model can't route around it. Hiding tools is only a convenience.

Separately from the mode, note that merges, bulk updates and public replies are real and immediate. Clients that honour MCP annotations will ask before destructive calls; the server's instructions also tell the model to confirm these with you.

## Configuration reference

| Variable | Default | |
|---|---|---|
| `ZENDESK_SUBDOMAIN` / `ZENDESK_DOMAIN` | — | One is required |
| `ZENDESK_EMAIL` + `ZENDESK_API_TOKEN` | — | Or `ZENDESK_CLIENT_ID` + `ZENDESK_CLIENT_SECRET` (OAuth client), or `ZENDESK_OAUTH_TOKEN` (access token) |
| `ZENDESK_OAUTH_SCOPE` | `read write` | Scope requested with an OAuth client (`read` in read_only mode) |
| `ZENDESK_ACCESS_MODE` | `full` | `full`, `no_delete`, `read_only` |
| `ZENDESK_FILE_ROOT` | unset | Directory for local uploads/downloads, CSV exports and the targets file. Unset = no local file access (text/base64 uploads and inline image/text downloads still work). `/data` in Docker. |
| `ZENDESK_TARGETS_FILE` | — | Override where report targets are stored |
| `ZENDESK_TOOLSETS` | `all` | `core`, `analytics`, `admin` (comma-separated) |
| `ZENDESK_JOURNAL` | `on` | `off` disables the change journal (and therefore revert) |
| `ZENDESK_JOURNAL_FILE` | — | Override where the change journal is written |
| `ZENDESK_MAX_RESPONSE_CHARS` | `60000` | Larger results are trimmed |
| `ZENDESK_DEFAULT_LIMIT` | `100` | |
| `ZENDESK_TIMEOUT` / `ZENDESK_MAX_RETRIES` | `30` / `4` | |
| `MCP_TRANSPORT` | `stdio` | or `http` |
| `MCP_HOST` / `MCP_PORT` / `MCP_PATH` | `127.0.0.1` / `8000` / `/mcp` | |
| `MCP_AUTH_TOKEN` | — | Required for non-localhost HTTP |
| `MCP_ALLOWED_HOSTS` | — | Optional Host-header allowlist |

## Example prompts

- "Show me all urgent open tickets in the Billing group updated in the last 2 days."
- "Summarise ticket 48211, look at the screenshot the customer attached, and draft an internal note with next steps."
- "Find every macro that sets the group to Tier 2 and list which ones are inactive."
- "Which triggers send email to requesters? Show their conditions."
- "Export tickets changed since Oct 1 and tell me the top 10 tags."
- "Give me last month's KPIs broken down by agent, in business hours."
- "What's our median and p90 time between a customer reply and the next agent reply this week, per agent? Which tickets are waiting longest right now?"
- "How did SLA achievement for first reply vs next reply compare in September?"
- "Build a Q3 support review deck for the whole company, with customer quotes and our biggest issues."
- "What were the top 5 rising contact reasons this month vs last, and do we have Help Center articles for them?"
- "When should we add evening coverage? Show the heatmap in Pacific time."
- "Profile Acme Corp: how much do they contact us, how fast are we for them compared with other customers, and how do they feel about us?"
- "Which of Globex's tickets this quarter were the hardest, and what went wrong on each?"
- "Which enterprise customers renewing in the next 90 days look unhealthy, and why?"
- "What have we promised customers this week that's now overdue? Who's been waiting longest for a reply?"
- "Brief me on ticket 48211 for a Slack post to engineering."
- "Is anything spreading across customers since yesterday's release?"
- "What questions do we answer the same way every time? Draft macros for the top three."
- "How often do tickets land in the wrong group, and which triggers are responsible?"
- "Will we keep up over the next six weeks if volume keeps rising?"
- "Document our current Zendesk configuration and list anything that looks broken or unused."
- "Here's our new architecture plan. Snapshot the config, turn the plan into a blueprint, and show me the plan before changing anything."
- "Add these five agents to Tier 1 with the Light Agent role." / "Offboard jane@acme.com and move her tickets to Tier 2."
- "What would break if we deleted the Billing group?"
- "Create a draft Help Center article in the Troubleshooting section from the resolution in ticket 48211."

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

The suite runs every tool against a fake Zendesk API (no network) and starts a real HTTP server to check auth.

Layout: `client.py` (auth, access enforcement, retries, pagination, journaling), `journal.py` (change journal), `formatting.py` (slimming, size limits, CSV), `metrics.py` (stats, business hours, reply-gap engine), `periods.py`, `privacy.py` (redaction), `targets.py` (definitions, deltas, RAG), `tools/admin.py`, `tools/blueprint.py` (plan/apply engine), `tools/changes.py` (snapshots, revert), `tools/docs.py` (configuration documentation), `tools/customers.py` (customer profiles), `tools/accounts.py` (account health, timelines), `tools/followups.py` (promises, ticket briefs), `tools/patterns.py` (emerging issues, deflection), `tools/operations.py` (routing, macros, agents, capacity), `textmine.py` (tokens, TF-IDF, clustering), `audits.py` (ticket change history), `tools/*.py` (one module per area), `server.py` (wiring), `__main__.py` (CLI and HTTP auth). To add a resource type, add a line to `RESOURCES` in `tools/resources.py`.

## License

MIT — see [LICENSE](LICENSE).
