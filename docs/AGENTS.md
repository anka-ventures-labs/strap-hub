# Connect strap-hub to a Hermes agent

Run `uv sync` in the hub checkout and initialize the SQLite store through the
hub's ingest, import, or WHOOP sync workflow. Add the following entry under
the Hermes profile's top-level `mcp_servers` mapping. Use absolute paths on
the machine where Hermes runs.

```yaml
mcp_servers:
  strap_hub:
    command: "/path/to/strap-hub/.venv/bin/strap-hub-mcp"
    args: []
    env:
      HUB_DB_PATH: "/path/to/data/hub.db"
      # HUB_TZ: "Etc/UTC"  # Optional IANA timezone override
      # HUB_WHOOP_TOKEN_PATH: "/path/to/token.json"  # Only for a custom location
```

Reload the Hermes profile so it spawns the server over stdio. The process needs
read access to the hub database and to the token file for WHOOP status. The
server uses the hub's local timezone for today and week boundaries by default.
`HUB_TZ` can override that timezone.
Band dates come from the phone or backup import. Cloud dates use WHOOP's recorded
offset.

| Tool | What it answers | Example question |
| --- | --- | --- |
| `data_freshness` | Last digest, band record and WHOOP sync times, local WHOOP token state, and coverage across the store. | “When was my band data last updated?” |
| `sleep_summary` | Nightly sleep metrics and averages for the past `days` calendar days including today, with changes from the previous equal period. Use 1–90 days. The default is 7. | “How did I sleep this week?” Use the number of days from Monday through today. |
| `sleep_night` | Stage minutes for a specific `date`. Band nights include a timeline and wake count. Cloud nights include the WHOOP disturbance count when available. | “How many times did I wake up on January 12?” |
| `workouts` | Workouts in the inclusive `from` and `to` range, newest first. The default range is local Monday through today. Use `limit` from 1 to 100 (default 50). Pass `next_cursor` as `cursor` with the same range for the next page. Counts and totals cover the whole range. | “What workouts did I do this week?” |
| `readiness_today` | Today's readiness, HRV, and resting heart rate, or the latest day with any of those metrics. Each metric is compared with its average over the 30 calendar days before the reported day. Yesterday's strain uses the hub's local date. | “How does my readiness today compare with my baseline?” |
| `metric_trend` | Daily readings plus mean, minimum, and maximum for one stored metric `key`, such as `rmssd`. Use 1–365 days ending today. The default is 30. Unknown keys retain stored precision. | “What was my resting heart rate trend over the past month?” |
| `strain_day` | Strain, energy in kJ, average and peak heart rate, and steps for a local `date`. | “How many steps did I take yesterday?” |
| `sleep_detail` | Main sleep, naps, stage minutes, sleep need, performance, consistency, efficiency, respiratory rate, and recovery for a wake `date`. | “How much extra sleep did WHOOP recommend after yesterday's workout?” |

Supply dates as `YYYY-MM-DD`. Each result identifies its `source` and includes
the associated freshness timestamp in `as_of`. Cloud metrics take precedence
where available, and dates or fields with band coverage fill gaps. When both
sources contribute to a range, each row identifies its own source. Use
`metric_trend` with `strain`, `steps`, `kilojoule`, `avg_hr`, or `max_hr` to compare
activity across days. MCP reads the already synced database.

**Privacy.** The tools read the local database, and Hermes sends tool results
to whatever model the profile uses. With a hosted model, those health summaries
go to the model provider.


## Gotcha: per-platform toolset allowlists

Hermes enables MCP servers per platform. If a profile's `platform_toolsets.<platform>`
list already names any MCP server (for example another server added earlier), that
list is an allowlist and a newly added server stays hidden on that platform. Add the
server name (`whoop`) to each such list. Platforms with no saved list get every
enabled MCP server. After `hermes mcp add`, open sessions need `/reload-mcp` or a new
session. Verify with a one-off session that asks the agent to call `data_freshness`.
