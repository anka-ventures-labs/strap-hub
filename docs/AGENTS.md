# Connect strap-hub to a Hermes agent

Run `uv sync` in the hub checkout and initialize the SQLite store through the
hub's ingest or import workflow. In the Hermes profile that should have access,
add the following entry under its top-level `mcp_servers` mapping. Replace both
paths with absolute paths on the machine where Hermes runs.

```yaml
mcp_servers:
  strap_hub:
    command: "/path/to/strap-hub/.venv/bin/strap-hub-mcp"
    args: []
    env:
      HUB_DB_PATH: "/path/to/data/hub.db"
      # HUB_TZ: "Etc/UTC"  # Optional IANA timezone override
```

Reload the Hermes profile so it spawns the server over stdio. The process needs
read access to the hub database. By default, the server uses the hub's local
timezone for today and week boundaries. `HUB_TZ` can override that timezone.
Stored date labels still come from the phone or the backup import.

| Tool | What it answers | Example question |
| --- | --- | --- |
| `data_freshness` | When the hub last received a digest or import, the latest band timestamp, and the number and newest date of stored days. | “When was my band data last updated?” |
| `sleep_summary` | Nightly sleep metrics and averages for the past `days` calendar days including today, with changes from the previous equal period. Use 1–90 days. The default is 7. | “How did I sleep this week?” Use the number of days from Monday through today. |
| `sleep_night` | The merged stage timeline, minutes by stage, and count of wake periods for a specific `date`. | “How many times did I wake up on January 12?” |
| `workouts` | Workouts in the inclusive `from` and `to` range, newest first. The default range is local Monday through today. Use `limit` from 1 to 100 (default 50). Pass `next_cursor` as `cursor` with the same range for the next page. Counts and totals cover the whole range. | “What workouts did I do this week?” |
| `readiness_today` | Today's readiness, HRV, and resting heart rate, or the latest day with any of those metrics. Each metric is compared with its average over the 30 calendar days before the reported day. Yesterday's strain uses the hub's local date. | “How does my readiness today compare with my baseline?” |
| `metric_trend` | Daily readings plus mean, minimum, and maximum for one stored metric `key`, such as `rmssd`. Use 1–365 days ending today. The default is 30. Unknown keys retain stored precision. | “What was my resting heart rate trend over the past month?” |

Supply dates as `YYYY-MM-DD`. Every result includes `as_of`, the latest band
record timestamp when known. A range with no matching records returns an empty
list. Averages use only recorded values. `sleep_summary` uses stored nightly
metrics, while `sleep_night` uses stored stage segments.

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
