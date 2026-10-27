# Telclaw database analysis over MCP

## Analysis and scope

The current source uses SQLite (`TELCLAW_DB_NAME`, normally `telclaw.db`).
`messages.id` joins the extraction tables through `processed_message_id`; the
Telegram `message_id` alone is not globally unique. Delivery status/attempt tables
explain what happened after extraction. A successful AI run is not proof of a
verified job, valid city, or successful API submission.

Existing reporting helpers sometimes initialize tables or recover pipeline states.
The MCP integration therefore uses a separate read-only connection, imports no
Telegram config, never calls migrations, and never starts the crawler or AI providers.
It needs no Telegram API credentials. The database must already exist.

The simplest useful interface is four tools:

| Tool | Purpose |
| --- | --- |
| `database_schema` | Discover available approved tables/columns |
| `query_database` | Run one bounded SELECT/CTE, joins and aggregate analysis |
| `inspect_message` | Read original/cleaned text, extracted fields and delivery records for a local ID |
| `pipeline_summary` | Group current states and error codes |

The client AI chooses queries and analyzes results. This server does not contain
an LLM or automatically analyze anything on a schedule. It exposes messages,
housing/job/transfer extraction, transfer locations, Jobs/Cargo delivery and
attempt tables, and shared request counts. It excludes configuration, subscriber,
activity, session and worker metadata tables. Media paths/references and worker
tokens are excluded even from SQL. Source text/contact information IS accessible
to the authorized client: connect only an AI/client you trust with that data.

## Install and run (same host as the database)

From your Telclaw checkout and Python 3.11+ virtual environment:

```bash
python -m pip install -r requirements-mcp.txt
python -m integrations.mcp_server --db /opt/Crawlerbot/Telclaw/telclaw.db
```

The default is stdio: silence while waiting for a client is normal. It opens no
network port. Use the actual database path if your crawler has `TELCLAW_DB_NAME`
set to a different file. The CLI reads that environment variable, but deliberately
does not load `.env`; pass `--db` explicitly. Stop with Ctrl+C. No schema or bot
configuration changes are necessary.

Example configuration for a desktop/CLI MCP client with `mcpServers` support
(replace paths with the actual virtualenv executable):

```json
{
  "mcpServers": {
    "telclaw": {
      "command": "/opt/Crawlerbot/Telclaw/venv/bin/python",
      "args": ["-m", "integrations.mcp_server", "--db", "/opt/Crawlerbot/Telclaw/telclaw.db"],
      "cwd": "/opt/Crawlerbot/Telclaw"
    }
  }
}
```

For an MCP client on your laptop, stdio can run through existing SSH access:

```json
{
  "mcpServers": {
    "telclaw": {
      "command": "ssh",
      "args": ["-T", "YOUR_USER@YOUR_SERVER", "cd /opt/Crawlerbot/Telclaw && exec /opt/Crawlerbot/Telclaw/venv/bin/python -m integrations.mcp_server --db /opt/Crawlerbot/Telclaw/telclaw.db"]
    }
  }
}
```

Authenticate SSH first; its noninteractive stdout must contain only MCP traffic.
The MCP client must support spawning local commands; this JSON is not a web-chat
connector setup and does not automatically connect this conversation.

## Optional HTTP for compatible remote clients

```bash
export TELCLAW_MCP_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m integrations.mcp_server --db /opt/Crawlerbot/Telclaw/telclaw.db --transport http --port 8790
```

Keep the generated token in your secret manager/service environment and configure
the client with the same token. The Streamable HTTP endpoint is
`http://127.0.0.1:8790/mcp` with header `Authorization: Bearer YOUR_TOKEN`.
It binds only to loopback and refuses HTTP startup without a 32-character token.
Use an SSH tunnel for remote private access, or an authenticated TLS reverse proxy
with the upstream Host set to `127.0.0.1:8790` (SDK DNS-rebinding protection remains
active). Do not expose the database or this unencrypted upstream publicly.

This minimal HTTP mode supports clients that can supply a static bearer header.
OAuth discovery/registration and direct compatibility with any particular hosted
chat service are not implemented or claimed. No public endpoint has been deployed.

## Ask the assistant

- Why are Jobs blocked? Group failure codes and examine several real examples.
- Compare original text, cleaned text and AI extraction for local ID 20466.
- Which channels produced the most verified Jobs this week?
- How many Cloudflare extraction errors remain? (Provider identity is only
  available if stored in the selected data; do not infer it from a generic error.)

Example query:

```sql
SELECT m.id, m.channel_username, m.message_id, m.ai_status, m.ai_error,
       j.is_job_listing, j.city, d.status, d.error_code
FROM messages m
LEFT JOIN joblist j ON j.processed_message_id = m.id
LEFT JOIN advertio_jobs_deliveries d ON d.processed_message_id = m.id
WHERE m.classification_category = ?
ORDER BY m.id DESC
```

Pass `parameters=["joblist"]`, then call `inspect_message` on selected IDs.
`SELECT * FROM messages` is deliberately denied because it includes hidden columns;
choose columns from `database_schema` instead. Results include column names and row
arrays (preserves duplicate column names), truncation flags and row count.

## Limits and verification

The database opens with `mode=ro` plus `query_only`; SQLite's authorizer restricts
reads to approved tables/columns and approved functions. Writes, PRAGMA, ATTACH,
extensions, arbitrary files and multiple SQL statements are unavailable. Each SQL
query has a two-second execution budget (plus up to one second lock wait), 12,000
SQL characters, at most 200 returned rows, 6,000 characters per cell and about
256 KB of row data per result. `inspect_message` combines several bounded results;
large text can be read in chunks with `substr`. Binary values are omitted.

No persistent transaction is held between calls; the running bot can keep writing.
Different calls can observe different committed states. Database content is
untrusted input, never instructions for the AI. The client controls what model
receives it. Database-reading errors become MCP tool errors, not fabricated success.

Tests exercise rejected writes/hidden reads, missing-file behavior, bounded and
live reads, source/extraction/delivery joins, a real stdio MCP session and an
HTTP tool call with valid/invalid bearer credentials. Run:

```bash
python -m pytest -q tests/test_database_mcp.py
```

Rollback: stop the separate MCP process; remove its client configuration. The
crawler does not import this optional integration. No data migration is involved.
