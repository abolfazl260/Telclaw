"""SQLite persistence for the Telclaw pipeline."""

import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone
import config

MESSAGE_COLUMNS = {"raw_text":"TEXT","cleaned_text":"TEXT","processing_status":"TEXT NOT NULL DEFAULT 'pending'","processing_started_at":"TEXT","collection_status":"TEXT NOT NULL DEFAULT 'collected'","ai_status":"TEXT NOT NULL DEFAULT 'waiting'","ai_started_at":"TEXT","pipeline_version":"TEXT","cleaned_at":"TEXT","ai_category":"TEXT","ai_processed_at":"TEXT","ai_error":"TEXT","channel_id":"INTEGER","channel_name":"TEXT","sender_id":"INTEGER","sender_username":"TEXT","sender_type":"TEXT","has_media":"INTEGER NOT NULL DEFAULT 0","media_type":"TEXT","file_unique_id":"TEXT","media_group_id":"TEXT","media_path":"TEXT","media_paths":"TEXT","message_link":"TEXT","media_reference":"TEXT","advertio_status":"TEXT NOT NULL DEFAULT 'waiting'","advertio_lead_id":"TEXT","advertio_error":"TEXT","advertio_processed_at":"TEXT","classification_status":"TEXT NOT NULL DEFAULT 'waiting'","classification_started_at":"TEXT","classification_category":"TEXT","classification_error":"TEXT","classification_processed_at":"TEXT","classification_attempts":"INTEGER NOT NULL DEFAULT 0"}
CATEGORY_TABLES = {"housinglist":{"property_type":"TEXT","listing_type":"TEXT","title":"TEXT","description":"TEXT","location":"TEXT","country_code":"TEXT","province":"TEXT","city":"TEXT","neighborhood":"TEXT","price":"REAL","currency":"TEXT","rent_period":"TEXT","bedrooms":"TEXT","bathrooms":"INTEGER","area":"REAL","area_unit":"TEXT","furnished":"TEXT","availability":"TEXT","property_condition":"TEXT","contact":"TEXT","features":"TEXT"},"transferlist":{"title":"TEXT","description":"TEXT","origin_city":"TEXT","origin_province":"TEXT","origin_country":"TEXT","destination_city":"TEXT","destination_province":"TEXT","destination_country":"TEXT","airline":"TEXT","flight_number":"TEXT","departure_date":"TEXT","departure_time":"TEXT","arrival_date":"TEXT","arrival_time":"TEXT","transport_type":"TEXT","cargo_type":"TEXT","weight":"REAL","weight_unit":"TEXT","quantity":"REAL","volume":"REAL","volume_unit":"TEXT","price":"REAL","currency":"TEXT","contact":"TEXT","features":"TEXT"},"joblist":{"job_title":"TEXT","company":"TEXT","location":"TEXT","employment_type":"TEXT","salary":"REAL","salary_currency":"TEXT","salary_period":"TEXT","experience":"TEXT","education":"TEXT","skills":"TEXT","remote":"INTEGER","job_type":"TEXT","description":"TEXT","application_method":"TEXT","contact":"TEXT"}}

def get_connection():
    db_path=Path(config.DB_NAME)
    if db_path.parent!=Path("."): db_path.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(str(db_path)); conn.row_factory=sqlite3.Row; conn.execute("PRAGMA foreign_keys = ON"); return conn

def _migrate_messages_table(cursor):
    cursor.execute("PRAGMA table_info(messages)"); existing={row[1] for row in cursor.fetchall()}
    for column,definition in MESSAGE_COLUMNS.items():
        if column not in existing: cursor.execute(f"ALTER TABLE messages ADD COLUMN {column} {definition}")
    cursor.execute("UPDATE messages SET collection_status=COALESCE(collection_status,'collected')")
    cursor.execute("UPDATE messages SET processing_status=CASE processing_status WHEN 'collected' THEN 'pending' WHEN 'processed' THEN 'processed' WHEN 'processing_failed' THEN 'failed' WHEN 'ai_processed' THEN 'processed' WHEN 'ai_failed' THEN 'processed' ELSE processing_status END")
    cursor.execute("UPDATE messages SET classification_status=CASE WHEN classification_status='processing' THEN 'processing' WHEN classification_category IS NOT NULL THEN 'processed' WHEN processing_status='processed' AND COALESCE(classification_status,'waiting')='waiting' THEN 'pending' ELSE COALESCE(classification_status,'waiting') END")
    # initialize_db() runs on every startup. Modern statuses are authoritative:
    # a failed extraction often has ai_error=NULL, and must NEVER become processed
    # merely because ai_processed_at is populated. The same applies to skipped,
    # pending and in-flight processing records. Only infer legacy/uninitialized
    # states when the record is eligible for extraction.
    cursor.execute("""UPDATE messages SET ai_status=CASE
        WHEN ai_status IN ('pending','processing','processed','failed','skipped')
            THEN ai_status
        WHEN processing_status='processed'
             AND classification_status='processed'
             AND classification_category IN ('housinglist','transferlist','joblist')
            THEN CASE
                WHEN ai_processed_at IS NULL THEN 'pending'
                WHEN ai_error LIKE 'skipped:%' THEN 'skipped'
                WHEN ai_error IS NOT NULL THEN 'failed'
                ELSE 'processed'
            END
        ELSE COALESCE(ai_status,'waiting')
        END
        WHERE ai_status IS NULL OR ai_status NOT IN
            ('pending','processing','processed','failed','skipped')""")
    cursor.execute("UPDATE messages SET processing_started_at=NULL WHERE processing_status <> 'processing' OR processing_status IS NULL")
    cursor.execute("UPDATE messages SET classification_started_at=NULL WHERE classification_status <> 'processing' OR classification_status IS NULL")
    cursor.execute("UPDATE messages SET ai_started_at=NULL WHERE ai_status <> 'processing' OR ai_status IS NULL")

def _create_category_table(cursor,table,fields):
    columns=["id INTEGER PRIMARY KEY AUTOINCREMENT","processed_message_id INTEGER NOT NULL UNIQUE",*[f"{n} {d}" for n,d in fields.items()],"created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP","FOREIGN KEY(processed_message_id) REFERENCES messages(id) ON DELETE CASCADE"]
    cursor.execute(f"CREATE TABLE {table} ({', '.join(columns)})")

def _rebuild_category_table(cursor,table,fields,existing_columns):
    temp_table=f"{table}_new"
    cursor.execute(f"DROP TABLE IF EXISTS {temp_table}")
    _create_category_table(cursor,temp_table,fields)
    desired_columns=["id","processed_message_id",*fields.keys(),"created_at"]
    copy_columns=[column for column in desired_columns if column in existing_columns]
    if copy_columns:
        column_sql=", ".join(copy_columns)
        select_expressions=[]
        for column in copy_columns:
            if table=="housinglist" and column=="bathrooms":
                select_expressions.append(
                    """CASE
                        WHEN CAST(bathrooms AS REAL) >= 1
                         AND CAST(bathrooms AS REAL) < 11
                        THEN CAST(CAST(bathrooms AS REAL) AS INTEGER)
                        ELSE NULL
                    END AS bathrooms"""
                )
            elif table=="housinglist" and column=="furnished":
                select_expressions.append(
                    """CASE
                        WHEN furnished IS NULL THEN NULL
                        WHEN LOWER(TRIM(CAST(furnished AS TEXT))) IN
                             ('1','true','yes','furnished') THEN 'furnished'
                        WHEN LOWER(TRIM(CAST(furnished AS TEXT))) IN
                             ('0','false','no','unfurnished') THEN 'unfurnished'
                        WHEN LOWER(TRIM(CAST(furnished AS TEXT))) IN
                             ('partial','partially','partially furnished') THEN 'partially'
                        ELSE NULL
                    END AS furnished"""
                )
            else:
                select_expressions.append(column)
        cursor.execute(
            f"INSERT INTO {temp_table} ({column_sql}) SELECT {', '.join(select_expressions)} FROM {table}"
        )
    cursor.execute(f"DROP TABLE {table}")
    cursor.execute(f"ALTER TABLE {temp_table} RENAME TO {table}")

def _create_category_tables(cursor):
    for table,fields in CATEGORY_TABLES.items():
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",(table,)); exists=cursor.fetchone() is not None
        if not exists:
            _create_category_table(cursor,table,fields)
        else:
            cursor.execute(f"PRAGMA table_info({table})")
            table_info=cursor.fetchall()
            existing={r[1] for r in table_info}
            existing_types={r[1]:str(r[2] or "").upper() for r in table_info}
            desired={"id","processed_message_id",*fields.keys(),"created_at"}
            type_mismatch=any(
                name in existing_types
                and existing_types[name] != str(definition).split()[0].upper()
                for name,definition in fields.items()
            )
            if (table=="transferlist" and existing!=desired) or type_mismatch:
                _rebuild_category_table(cursor,table,fields,existing)
            else:
                for n,d in fields.items():
                    if n not in existing: cursor.execute(f"ALTER TABLE {table} ADD COLUMN {n} {d}")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_processed_message ON {table}(processed_message_id)")

def initialize_db():
    conn=get_connection()
    try:
        cursor=conn.cursor(); cursor.execute("""CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_username TEXT NOT NULL, message_id INTEGER NOT NULL, text TEXT, raw_text TEXT, cleaned_text TEXT, date TEXT NOT NULL, media_path TEXT, message_link TEXT, media_reference TEXT, collection_status TEXT NOT NULL DEFAULT 'collected', processing_status TEXT NOT NULL DEFAULT 'pending', ai_status TEXT NOT NULL DEFAULT 'waiting', pipeline_version TEXT, cleaned_at TEXT, ai_category TEXT, ai_processed_at TEXT, ai_error TEXT, channel_id INTEGER, channel_name TEXT, sender_id INTEGER, sender_username TEXT, sender_type TEXT, has_media INTEGER NOT NULL DEFAULT 0, media_type TEXT, file_unique_id TEXT, media_group_id TEXT, media_paths TEXT, advertio_status TEXT NOT NULL DEFAULT 'waiting', advertio_lead_id TEXT, advertio_error TEXT, advertio_processed_at TEXT, classification_status TEXT NOT NULL DEFAULT 'waiting', classification_category TEXT, classification_error TEXT, classification_processed_at TEXT, classification_attempts INTEGER NOT NULL DEFAULT 0, UNIQUE(channel_username,message_id))""")
        _migrate_messages_table(cursor)
        for sql in ("CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date)","CREATE INDEX IF NOT EXISTS idx_messages_channel_date ON messages(channel_username,date)","CREATE INDEX IF NOT EXISTS idx_messages_collection_status ON messages(collection_status)","CREATE INDEX IF NOT EXISTS idx_messages_processing_status ON messages(processing_status)","CREATE INDEX IF NOT EXISTS idx_messages_classification_status ON messages(classification_status)","CREATE INDEX IF NOT EXISTS idx_messages_ai_status ON messages(ai_status)","CREATE INDEX IF NOT EXISTS idx_messages_media_type ON messages(media_type)","CREATE INDEX IF NOT EXISTS idx_messages_media_group_id ON messages(media_group_id)","CREATE INDEX IF NOT EXISTS idx_messages_ai_category ON messages(ai_category)","CREATE INDEX IF NOT EXISTS idx_messages_sender_id ON messages(sender_id)","CREATE INDEX IF NOT EXISTS idx_messages_advertio_status ON messages(advertio_status)"): cursor.execute(sql)
        cursor.execute("CREATE TABLE IF NOT EXISTS crawler_settings (channel_username TEXT PRIMARY KEY,target_date TEXT NOT NULL,last_crawled_date TEXT)")
        cursor.execute("CREATE TABLE IF NOT EXISTS telegram_monitor_subscribers (chat_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, enabled INTEGER NOT NULL DEFAULT 1, first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        cursor.execute("""CREATE TABLE IF NOT EXISTS system_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'INFO',
            source TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            details TEXT,
            created_at TEXT NOT NULL
        )""")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_system_activity_created ON system_activity(created_at DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_system_activity_kind ON system_activity(kind, created_at DESC)")
        _create_category_tables(cursor); conn.commit()
    finally: conn.close()
    recover_stale_processing_states()

def recover_stale_processing_states(timeout_seconds=None):
    """Return abandoned stage claims to their queues during application startup."""
    timeout = float(
        config.QUEUE_PROCESSING_TIMEOUT_SECONDS
        if timeout_seconds is None else timeout_seconds
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(0.0, timeout))).isoformat()
    recovered = {}
    conn = get_connection()
    try:
        recovered["processing"] = conn.execute(
            """UPDATE messages
               SET processing_status='pending', processing_started_at=NULL
               WHERE processing_status='processing'
                 AND (processing_started_at IS NULL OR datetime(processing_started_at) < datetime(?))""",
            (cutoff,),
        ).rowcount
        recovered["classification"] = conn.execute(
            """UPDATE messages
               SET classification_status='pending',
                   classification_started_at=NULL,
                   classification_error=NULL
               WHERE classification_status='processing'
                 AND processing_status='processed'
                 AND (classification_started_at IS NULL OR datetime(classification_started_at) < datetime(?))""",
            (cutoff,),
        ).rowcount
        recovered["ai"] = conn.execute(
            """UPDATE messages
               SET ai_status='pending', ai_started_at=NULL, ai_error=NULL
               WHERE ai_status='processing'
                 AND processing_status='processed'
                 AND classification_status='processed'
                 AND classification_category IN ('housinglist','transferlist','joblist')
                 AND (ai_started_at IS NULL OR datetime(ai_started_at) < datetime(?))""",
            (cutoff,),
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    total = sum(recovered.values())
    if total:
        record_system_activity(
            "queue_recovery",
            "WARNING",
            "storage.database",
            "Recovered stale processing claims during startup",
            {"timeout_seconds": timeout, "recovered": recovered},
        )
    return recovered

def subscribe_monitor_chat(chat_id,username=None,first_name=None):
    conn=get_connection()
    try: conn.execute("INSERT INTO telegram_monitor_subscribers(chat_id,username,first_name,enabled) VALUES(?,?,?,1) ON CONFLICT(chat_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name,enabled=1,last_seen=CURRENT_TIMESTAMP",(int(chat_id),username,first_name)); conn.commit()
    finally: conn.close()
def unsubscribe_monitor_chat(chat_id):
    conn=get_connection()
    try: conn.execute("UPDATE telegram_monitor_subscribers SET enabled=0,last_seen=CURRENT_TIMESTAMP WHERE chat_id=?",(int(chat_id),)); conn.commit()
    finally: conn.close()
def get_monitor_subscribers():
    conn=get_connection()
    try: return [dict(r) for r in conn.execute("SELECT * FROM telegram_monitor_subscribers WHERE enabled=1 ORDER BY chat_id").fetchall()]
    finally: conn.close()

def record_system_activity(kind, level="INFO", source="", message="", details=None):
    """Persist one operational event for the back-office health timeline."""
    kind=str(kind or "activity").strip()[:80]
    level=str(level or "INFO").strip().upper()[:20]
    source=str(source or "").strip()[:160]
    message=str(message or "").strip()[:4000]
    if details is None:
        encoded=None
    elif isinstance(details,str):
        encoded=details[:12000]
    else:
        encoded=json.dumps(details,ensure_ascii=False,default=str)[:12000]
    conn=get_connection()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS system_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'INFO',
            source TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            details TEXT,
            created_at TEXT NOT NULL
        )""")
        conn.execute("""INSERT INTO system_activity(kind,level,source,message,details,created_at)
            VALUES(?,?,?,?,?,?)""",
            (kind,level,source,message,encoded,datetime.now(timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()


def get_recent_system_activity(limit=60):
    conn=get_connection()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS system_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'INFO',
            source TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            details TEXT,
            created_at TEXT NOT NULL
        )""")
        return [dict(r) for r in conn.execute("""SELECT * FROM system_activity
            ORDER BY id DESC LIMIT ?""",(min(max(int(limit),1),200),)).fetchall()]
    finally:
        conn.close()

def get_pipeline_status():
    """Legacy /status API, using the same queue-eligibility metrics as /health."""
    from services.health_metrics import collect
    conn = get_connection()
    try:
        metrics = collect(conn)
        row = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE collection_status='collected'"
        ).fetchone()
        subscribers = conn.execute(
            "SELECT COUNT(*) FROM telegram_monitor_subscribers WHERE enabled=1"
        ).fetchone()[0]
        return {
            "system": "RUNNING",
            "total_messages": metrics["total"],
            "collected": int(row[0] or 0),
            **{key: metrics[key] for key in (
                "processing_pending", "processing_failed",
                "classification_pending", "classification_failed",
                "ai_pending", "ai_failed", "advertio_pending", "advertio_failed",
            )},
            "channels": metrics["channels"],
            "subscribers": int(subscribers or 0),
            "last_crawl": metrics["last_crawl"],
        }
    finally:
        conn.close()

def get_classification_queue_status():
    """Return current AI classification counts directly from SQLite."""
    conn=get_connection()
    try:
        row=conn.execute("""SELECT
            SUM(CASE WHEN classification_status='pending' THEN 1 ELSE 0 END) pending,
            SUM(CASE WHEN classification_status='processing' THEN 1 ELSE 0 END) processing,
            SUM(CASE WHEN classification_status='processed' THEN 1 ELSE 0 END) classified,
            SUM(CASE WHEN classification_status='failed' THEN 1 ELSE 0 END) failed
            FROM messages""").fetchone()
        return {name:int(row[name] or 0) for name in ("pending", "processing", "classified", "failed")}
    finally: conn.close()

def retry_failed_classifications():
    """Put all failed classifications back into the queue for a manual retry."""
    conn=get_connection()
    try:
        cursor=conn.execute("""UPDATE messages
            SET classification_status='pending', classification_error=NULL,
                classification_processed_at=NULL, classification_attempts=0
            WHERE classification_status='failed'""")
        conn.commit()
        return cursor.rowcount
    finally: conn.close()

def _last_time(conn,where,params=()):
    row=conn.execute(f"SELECT MAX(date) value FROM messages WHERE {where}",params).fetchone(); return row["value"] if row and row["value"] else None

def get_pipeline_health():
    """Unified health metrics for the Telegram monitor and other front ends."""
    from services.health_metrics import collect, as_telegram_health
    conn = get_connection()
    try:
        return as_telegram_health(collect(conn))
    finally:
        conn.close()

def insert_message(channel_username,message_id,text,date_str,*,raw_text=None,cleaned_text=None,collection_status="collected",processing_status="pending",ai_status="waiting",pipeline_version=None,cleaned_at=None,channel_id=None,channel_name=None,sender_id=None,sender_username=None,sender_type=None,has_media=False,media_type=None,file_unique_id=None,media_group_id=None,media_path=None,media_paths=None,message_link=None,media_reference=None):
    conn=get_connection()
    try:
        encoded_paths = json.dumps(media_paths,ensure_ascii=False,separators=(",",":")) if isinstance(media_paths,(list,tuple)) else media_paths
        cursor=conn.execute("INSERT OR IGNORE INTO messages(channel_username,message_id,text,raw_text,cleaned_text,date,media_path,media_paths,message_link,media_reference,collection_status,processing_status,ai_status,pipeline_version,cleaned_at,channel_id,channel_name,sender_id,sender_username,sender_type,has_media,media_type,file_unique_id,media_group_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(channel_username,message_id,text,raw_text,cleaned_text,date_str,media_path,encoded_paths,message_link,media_reference,collection_status,processing_status,ai_status,pipeline_version,cleaned_at,channel_id,channel_name,sender_id,sender_username,sender_type,int(bool(has_media)),media_type,str(file_unique_id) if file_unique_id is not None else None,str(media_group_id) if media_group_id is not None else None)); conn.commit(); return cursor.rowcount>0
    finally: conn.close()

def get_latest_message_id(channel_username):
    conn=get_connection()
    try:
        row=conn.execute("SELECT MAX(message_id) max_message_id FROM messages WHERE channel_username=?",(channel_username,)).fetchone(); return int(row["max_message_id"]) if row and row["max_message_id"] is not None else 0
    finally: conn.close()

def _get_messages(where,params,limit,channel_username=None):
    conn=get_connection()
    try:
        sql="SELECT * FROM messages WHERE "+where; values=list(params)
        if channel_username: sql += " AND channel_username=?"; values.append(channel_username)
        sql += " ORDER BY id LIMIT ?"; values.append(int(limit)); return [dict(r) for r in conn.execute(sql,values).fetchall()]
    finally: conn.close()
def get_messages_by_status(status,limit=500,channel_username=None): return _get_messages("processing_status=?",[status],limit,channel_username)
def get_processing_pending_messages(limit=500,channel_username=None): return _get_messages("collection_status='collected' AND processing_status='pending'",[],limit,channel_username)
def get_classification_pending_messages(limit=50,channel_username=None): return _get_messages("processing_status='processed' AND NULLIF(TRIM(ai_category), '') IS NULL AND (classification_status='pending' OR (classification_status='failed' AND classification_attempts<?))",[config.AI_CLASSIFICATION_MAX_RETRIES],limit,channel_username)
def get_ai_pending_messages(limit=100,channel_username=None): return _get_messages("processing_status='processed' AND classification_status='processed' AND classification_category IN ('housinglist','transferlist','joblist') AND ai_status='pending'",[],limit,channel_username)
def get_advertio_pending_messages(limit=100,channel_username=None,before_datetime=None):
    conn=get_connection()
    try:
        sql="SELECT m.*,h.* FROM messages m INNER JOIN housinglist h ON h.processed_message_id=m.id WHERE m.processing_status='processed' AND m.ai_status='processed' AND m.ai_category='housinglist' AND COALESCE(m.advertio_status,'waiting') IN ('waiting','retry')"; values=[]
        if before_datetime:
            sql += """ AND (
                (COALESCE(m.advertio_status,'waiting')='retry' AND (m.advertio_processed_at IS NULL OR m.advertio_processed_at < ?))
                OR
                (COALESCE(m.advertio_status,'waiting')='waiting' AND (m.ai_processed_at IS NULL OR m.ai_processed_at < ?))
            )"""
            values.extend([before_datetime,before_datetime])
        if channel_username: sql += " AND m.channel_username=?"; values.append(channel_username)
        sql += " ORDER BY m.id LIMIT ?"; values.append(int(limit)); rows=conn.execute(sql,values).fetchall(); results=[]
        for row in rows:
            item=dict(row); item["housing_data"]={field:item.get(field) for field in CATEGORY_TABLES["housinglist"]}; results.append(item)
        return results
    finally: conn.close()
def get_previous_messages_by_sender(sender_id,before_id):
    if sender_id is None:return []
    conn=get_connection()
    try:return [dict(r) for r in conn.execute("SELECT id,message_id,channel_username,sender_id,sender_username,raw_text,text FROM messages WHERE sender_id=? AND id<? AND COALESCE(raw_text,text,'')<>'' ORDER BY id",(sender_id,before_id)).fetchall()]
    finally: conn.close()
def claim_processing_message(message_id, channel_username):
    conn = get_connection()
    try:
        cursor = conn.execute(
            """UPDATE messages
               SET processing_status='processing', processing_started_at=?
               WHERE channel_username=? AND message_id=?
                 AND collection_status='collected' AND processing_status='pending'""",
            (datetime.now(timezone.utc).isoformat(), channel_username, message_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()

def claim_classification_message(message_id, channel_username, max_retries):
    conn = get_connection()
    try:
        cursor = conn.execute(
            """UPDATE messages
               SET classification_status='processing',
                   classification_started_at=?,
                   classification_error=NULL
               WHERE channel_username=? AND message_id=?
                 AND processing_status='processed'
                 AND NULLIF(TRIM(ai_category), '') IS NULL
                 AND (classification_status='pending'
                      OR (classification_status='failed' AND classification_attempts<?))""",
            (datetime.now(timezone.utc).isoformat(), channel_username, message_id, int(max_retries)),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()

def claim_ai_message(message_id, channel_username):
    conn = get_connection()
    try:
        cursor = conn.execute(
            """UPDATE messages
               SET ai_status='processing', ai_started_at=?, ai_error=NULL
               WHERE channel_username=? AND message_id=?
                 AND processing_status='processed'
                 AND classification_status='processed'
                 AND classification_category IN ('housinglist','transferlist','joblist')
                 AND ai_status='pending'""",
            (datetime.now(timezone.utc).isoformat(), channel_username, message_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()

def update_message(message_id,channel_username,**fields):
    allowed={"cleaned_text","text","collection_status","processing_status","processing_started_at","classification_status","classification_started_at","classification_category","classification_error","classification_processed_at","classification_attempts","ai_status","ai_started_at","pipeline_version","cleaned_at","ai_category","ai_processed_at","ai_error","advertio_status","advertio_lead_id","advertio_error","advertio_processed_at","media_path","media_paths","media_group_id"}; updates={k:v for k,v in fields.items() if k in allowed}
    if not updates:return False
    assignments=", ".join(f"{k}=?" for k in updates); values=list(updates.values())+[channel_username,message_id]; conn=get_connection()
    try: cursor=conn.execute(f"UPDATE messages SET {assignments} WHERE channel_username=? AND message_id=?",values); conn.commit(); return cursor.rowcount>0
    finally: conn.close()
def delete_message(message_id,channel_username):
    conn=get_connection()
    try: cursor=conn.execute("DELETE FROM messages WHERE channel_username=? AND message_id=?",(channel_username,message_id)); conn.commit(); return cursor.rowcount>0
    finally: conn.close()
def update_processed_message(message_id,channel_username,**fields): return update_message(message_id,channel_username,**fields)
def _serialize_category_value(field,value):
    """Convert structured AI values to SQLite-safe representations without changing the schema."""
    if value is None:
        return None
    if field == "remote":
        if isinstance(value, bool):
            return int(value)
        return value
    if isinstance(value,(dict,list,tuple)):
        return json.dumps(value,ensure_ascii=False,separators=(",",":"))
    return value

def save_category_record(processed_message_id,category,data):
    if category not in CATEGORY_TABLES: raise ValueError(f"Unsupported category: {category}")
    fields=CATEGORY_TABLES[category]; columns=["processed_message_id"]+list(fields); values=[processed_message_id]
    for field in fields:
        value=_serialize_category_value(field,data.get(field))
        values.append(value)
    placeholders=", ".join("?" for _ in columns); assignments=", ".join(f"{f}=excluded.{f}" for f in fields); conn=get_connection()
    try: conn.execute(f"INSERT INTO {category}({', '.join(columns)}) VALUES({placeholders}) ON CONFLICT(processed_message_id) DO UPDATE SET {assignments}",values); conn.commit()
    finally: conn.close()
def get_category_record(processed_message_id,category):
    if category not in CATEGORY_TABLES: raise ValueError(f"Unsupported category: {category}")
    conn=get_connection()
    try:
        row=conn.execute(f"SELECT * FROM {category} WHERE processed_message_id=?",(processed_message_id,)).fetchone(); return dict(row) if row else None
    finally: conn.close()
def set_channel_target_date(channel_username,target_date_str):
    conn=get_connection()
    try: conn.execute("INSERT INTO crawler_settings(channel_username,target_date) VALUES(?,?) ON CONFLICT(channel_username) DO UPDATE SET target_date=excluded.target_date",(channel_username,target_date_str)); conn.commit()
    finally: conn.close()
def get_channel_target_date(channel_username):
    conn=get_connection()
    try:
        row=conn.execute("SELECT target_date FROM crawler_settings WHERE channel_username=?",(channel_username,)).fetchone(); return row["target_date"] if row else None
    finally: conn.close()
