"""Additive DDL, explicitly invoked at startup, never during crawl/claim."""

STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS cm_rules (
        id INTEGER PRIMARY KEY,
        source_peer_id INTEGER NOT NULL, source_name TEXT NOT NULL,
        destination_chat_id INTEGER NOT NULL, destination_username TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'disabled' CHECK(state IN ('disabled','active','paused')),
        mode TEXT NOT NULL DEFAULT 'media_only' CHECK(mode='media_only'),
        footer TEXT NOT NULL DEFAULT '', activation_watermark TEXT,
        created_by TEXT NOT NULL, updated_by TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(source_peer_id,destination_chat_id),
        CHECK(state='disabled' OR activation_watermark IS NOT NULL))""",
    """CREATE TABLE IF NOT EXISTS cm_posts (
        id INTEGER PRIMARY KEY, source_peer_id INTEGER NOT NULL, identity TEXT NOT NULL,
        grouped_id INTEGER, crawl_run_id TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(source_peer_id,identity))""",
    """CREATE TABLE IF NOT EXISTS cm_source_items (
        id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL REFERENCES cm_posts(id),
        source_peer_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
        message_date TEXT NOT NULL, grouped_id INTEGER,
        media_kind TEXT NOT NULL CHECK(media_kind IN ('photo','video','document')),
        position INTEGER NOT NULL, crawl_run_id TEXT NOT NULL,
        media_metadata TEXT NOT NULL DEFAULT '{}',
        UNIQUE(source_peer_id,message_id), UNIQUE(post_id,position))""",
    """CREATE TABLE IF NOT EXISTS cm_deliveries (
        id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL REFERENCES cm_posts(id),
        rule_id INTEGER NOT NULL REFERENCES cm_rules(id), destination_chat_id INTEGER NOT NULL,
        destination_username TEXT NOT NULL, caption TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'waiting'
          CHECK(status IN ('waiting','sending','sent','retry','failed','uncertain','skipped')),
        attempts INTEGER NOT NULL DEFAULT 0, attempt_limit INTEGER NOT NULL DEFAULT 3,
        claim_token TEXT, lease_until TEXT, next_retry TEXT,
        destination_message_ids TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT,
        UNIQUE(post_id,destination_chat_id))""",
    """CREATE TABLE IF NOT EXISTS cm_delivery_attempts (
        id INTEGER PRIMARY KEY, delivery_id INTEGER NOT NULL REFERENCES cm_deliveries(id),
        number INTEGER NOT NULL, claim_token TEXT NOT NULL UNIQUE,
        started_at TEXT NOT NULL, finished_at TEXT,
        outcome TEXT NOT NULL DEFAULT 'sending'
          CHECK(outcome IN ('sending','sent','retry','failed','uncertain','skipped')),
        error TEXT NOT NULL DEFAULT '', destination_message_ids TEXT NOT NULL DEFAULT '[]',
        UNIQUE(delivery_id,number))""",
    """CREATE TABLE IF NOT EXISTS cm_audit (
        id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
        entity_id INTEGER NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS cm_rules_source ON cm_rules(source_peer_id,state)",
    "CREATE INDEX IF NOT EXISTS cm_items_post ON cm_source_items(post_id,position)",
    "CREATE INDEX IF NOT EXISTS cm_queue_due ON cm_deliveries(status,next_retry,id)",
    "CREATE INDEX IF NOT EXISTS cm_queue_rule ON cm_deliveries(rule_id,status)",
    "CREATE INDEX IF NOT EXISTS cm_queue_lease ON cm_deliveries(status,lease_until)",
    "CREATE INDEX IF NOT EXISTS cm_audit_entity ON cm_audit(entity_id,id)",
    """CREATE TABLE IF NOT EXISTS cm_crawl_runs (
        id TEXT PRIMARY KEY, source_name TEXT NOT NULL, source_peer_id INTEGER,
        from_date TEXT NOT NULL, to_date TEXT NOT NULL, crawl_mode TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'running'
          CHECK(state IN ('running','completed','failed','stopped','interrupted','mirror_failed')),
        rules_snapshot TEXT NOT NULL DEFAULT '[]',
        started_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT,
        lease_until TEXT, error TEXT NOT NULL DEFAULT '', deliveries_created INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS cm_crawl_items (
        run_id TEXT NOT NULL REFERENCES cm_crawl_runs(id), message_id INTEGER NOT NULL,
        message_date TEXT NOT NULL, grouped_id INTEGER, media_kind TEXT NOT NULL,
        media_metadata TEXT NOT NULL, album_complete INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(run_id,message_id))""",
    """CREATE TABLE IF NOT EXISTS cm_dispatch_events (
        id INTEGER PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES cm_crawl_runs(id),
        source_peer_id INTEGER NOT NULL, deliveries_created INTEGER NOT NULL,
        created_at TEXT NOT NULL, acknowledged_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS cm_crawl_source ON cm_crawl_runs(source_peer_id,started_at DESC)",
    "CREATE INDEX IF NOT EXISTS cm_crawl_name ON cm_crawl_runs(source_name,started_at DESC)",
    "CREATE INDEX IF NOT EXISTS cm_crawl_lease ON cm_crawl_runs(state,lease_until)",
    "CREATE INDEX IF NOT EXISTS cm_crawl_groups ON cm_crawl_items(run_id,grouped_id,message_id)",
    "CREATE INDEX IF NOT EXISTS cm_dispatch_pending ON cm_dispatch_events(acknowledged_at,id)",
)
