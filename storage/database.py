"""SQLite persistence for the Telclaw pipeline."""

import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
import config

MESSAGE_COLUMNS = {"raw_text":"TEXT","cleaned_text":"TEXT","processing_status":"TEXT NOT NULL DEFAULT 'pending'","collection_status":"TEXT NOT NULL DEFAULT 'collected'","ai_status":"TEXT NOT NULL DEFAULT 'waiting'","pipeline_version":"TEXT","cleaned_at":"TEXT","ai_category":"TEXT","ai_processed_at":"TEXT","ai_error":"TEXT","channel_id":"INTEGER","channel_name":"TEXT","sender_id":"INTEGER","sender_username":"TEXT","sender_type":"TEXT","has_media":"INTEGER NOT NULL DEFAULT 0","media_type":"TEXT","file_unique_id":"TEXT","message_link":"TEXT","media_reference":"TEXT","advertio_status":"TEXT NOT NULL DEFAULT 'waiting'","advertio_lead_id":"TEXT","advertio_error":"TEXT","advertio_processed_at":"TEXT","classification_status":"TEXT NOT NULL DEFAULT 'waiting'","classification_category":"TEXT","classification_error":"TEXT","classification_processed_at":"TEXT","classification_attempts":"INTEGER NOT NULL DEFAULT 0"}
CATEGORY_TABLES = {"housinglist":{"property_type":"TEXT","listing_type":"TEXT","title":"TEXT","description":"TEXT","location":"TEXT","country_code":"TEXT","province":"TEXT","city":"TEXT","neighborhood":"TEXT","price":"REAL","currency":"TEXT","rent_period":"TEXT","bedrooms":"INTEGER","bathrooms":"REAL","area":"REAL","area_unit":"TEXT","furnished":"INTEGER","availability":"TEXT","property_condition":"TEXT","contact":"TEXT","features":"TEXT"},"transferlist":{"title":"TEXT","description":"TEXT","origin_city":"TEXT","origin_province":"TEXT","origin_country":"TEXT","destination_city":"TEXT","destination_province":"TEXT","destination_country":"TEXT","airline":"TEXT","flight_number":"TEXT","departure_date":"TEXT","departure_time":"TEXT","arrival_date":"TEXT","arrival_time":"TEXT","transport_type":"TEXT","cargo_type":"TEXT","weight":"REAL","weight_unit":"TEXT","quantity":"REAL","price":"REAL","currency":"TEXT","contact":"TEXT","features":"TEXT"},"joblist":{"job_title":"TEXT","company":"TEXT","location":"TEXT","employment_type":"TEXT","salary":"REAL","salary_currency":"TEXT","salary_period":"TEXT","experience":"TEXT","education":"TEXT","skills":"TEXT","remote":"INTEGER","job_type":"TEXT","description":"TEXT","application_method":"TEXT","contact":"TEXT"}}

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
    cursor.execute("UPDATE messages SET classification_status=CASE WHEN classification_category IS NOT NULL THEN 'processed' WHEN processing_status='processed' AND COALESCE(classification_status,'waiting')='waiting' THEN 'pending' ELSE COALESCE(classification_status,'waiting') END")
    cursor.execute("UPDATE messages SET ai_status=CASE WHEN processing_status='processed' AND ai_category IS NOT NULL THEN 'processed' WHEN ai_processed_at IS NOT NULL AND ai_error IS NOT NULL THEN 'failed' WHEN ai_processed_at IS NOT NULL THEN 'processed' ELSE COALESCE(ai_status,'waiting') END")

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
        cursor.execute(f"INSERT INTO {temp_table} ({column_sql}) SELECT {column_sql} FROM {table}")
    cursor.execute(f"DROP TABLE {table}")
    cursor.execute(f"ALTER TABLE {temp_table} RENAME TO {table}")

def _create_category_tables(cursor):
    for table,fields in CATEGORY_TABLES.items():
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",(table,)); exists=cursor.fetchone() is not None
        if not exists:
            _create_category_table(cursor,table,fields)
        else:
            cursor.execute(f"PRAGMA table_info({table})"); existing={r[1] for r in cursor.fetchall()}
            desired={"id","processed_message_id",*fields.keys(),"created_at"}
            if table=="transferlist" and existing!=desired:
                _rebuild_category_table(cursor,table,fields,existing)
            else:
                for n,d in fields.items():
                    if n not in existing: cursor.execute(f"ALTER TABLE {table} ADD COLUMN {n} {d}")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_processed_message ON {table}(processed_message_id)")

def initialize_db():
    conn=get_connection()
    try:
        cursor=conn.cursor(); cursor.execute("""CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_username TEXT NOT NULL, message_id INTEGER NOT NULL, text TEXT, raw_text TEXT, cleaned_text TEXT, date TEXT NOT NULL, media_path TEXT, message_link TEXT, media_reference TEXT, collection_status TEXT NOT NULL DEFAULT 'collected', processing_status TEXT NOT NULL DEFAULT 'pending', ai_status TEXT NOT NULL DEFAULT 'waiting', pipeline_version TEXT, cleaned_at TEXT, ai_category TEXT, ai_processed_at TEXT, ai_error TEXT, channel_id INTEGER, channel_name TEXT, sender_id INTEGER, sender_username TEXT, sender_type TEXT, has_media INTEGER NOT NULL DEFAULT 0, media_type TEXT, file_unique_id TEXT, advertio_status TEXT NOT NULL DEFAULT 'waiting', advertio_lead_id TEXT, advertio_error TEXT, advertio_processed_at TEXT, classification_status TEXT NOT NULL DEFAULT 'waiting', classification_category TEXT, classification_error TEXT, classification_processed_at TEXT, classification_attempts INTEGER NOT NULL DEFAULT 0, UNIQUE(channel_username,message_id))""")
        _migrate_messages_table(cursor)
        for sql in ("CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date)","CREATE INDEX IF NOT EXISTS idx_messages_channel_date ON messages(channel_username,date)","CREATE INDEX IF NOT EXISTS idx_messages_collection_status ON messages(collection_status)","CREATE INDEX IF NOT EXISTS idx_messages_processing_status ON messages(processing_status)","CREATE INDEX IF NOT EXISTS idx_messages_classification_status ON messages(classification_status)","CREATE INDEX IF NOT EXISTS idx_messages_ai_status ON messages(ai_status)","CREATE INDEX IF NOT EXISTS idx_messages_media_type ON messages(media_type)","CREATE INDEX IF NOT EXISTS idx_messages_ai_category ON messages(ai_category)","CREATE INDEX IF NOT EXISTS idx_messages_sender_id ON messages(sender_id)","CREATE INDEX IF NOT EXISTS idx_messages_advertio_status ON messages(advertio_status)"): cursor.execute(sql)
        cursor.execute("CREATE TABLE IF NOT EXISTS crawler_settings (channel_username TEXT PRIMARY KEY,target_date TEXT NOT NULL,last_crawled_date TEXT)")
        cursor.execute("CREATE TABLE IF NOT EXISTS telegram_monitor_subscribers (chat_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, enabled INTEGER NOT NULL DEFAULT 1, first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        _create_category_tables(cursor); conn.commit()
    finally: conn.close()

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

def get_pipeline_status():
    conn=get_connection()
    try:
        row=conn.execute("""SELECT COUNT(*) total_messages,SUM(CASE WHEN collection_status='collected' THEN 1 ELSE 0 END) collected,SUM(CASE WHEN collection_status='collected' AND processing_status='pending' THEN 1 ELSE 0 END) processing_pending,SUM(CASE WHEN processing_status='failed' THEN 1 ELSE 0 END) processing_failed,SUM(CASE WHEN processing_status='processed' AND classification_status='pending' THEN 1 ELSE 0 END) classification_pending,SUM(CASE WHEN classification_status='failed' THEN 1 ELSE 0 END) classification_failed,SUM(CASE WHEN processing_status='processed' AND ai_status='pending' THEN 1 ELSE 0 END) ai_pending,SUM(CASE WHEN ai_status='failed' THEN 1 ELSE 0 END) ai_failed,SUM(CASE WHEN COALESCE(advertio_status,'waiting') IN ('waiting','retry') AND ai_status='processed' THEN 1 ELSE 0 END) advertio_pending,SUM(CASE WHEN advertio_status='failed' THEN 1 ELSE 0 END) advertio_failed FROM messages""").fetchone()
        channels=conn.execute("SELECT COUNT(DISTINCT channel_username) n FROM messages").fetchone()["n"] or 0; subscribers=conn.execute("SELECT COUNT(*) n FROM telegram_monitor_subscribers WHERE enabled=1").fetchone()["n"] or 0
        def value(name): return int(row[name] or 0)
        return {"system":"RUNNING","total_messages":value("total_messages"),"collected":value("collected"),"processing_pending":value("processing_pending"),"processing_failed":value("processing_failed"),"classification_pending":value("classification_pending"),"classification_failed":value("classification_failed"),"ai_pending":value("ai_pending"),"ai_failed":value("ai_failed"),"advertio_pending":value("advertio_pending"),"advertio_failed":value("advertio_failed"),"channels":int(channels),"subscribers":int(subscribers)}
    finally: conn.close()

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
    """Return a DB-backed health snapshot. It never claims a worker is alive unless its DB activity is recent."""
    conn=get_connection()
    try:
        now=datetime.now(timezone.utc)
        last_crawl=_last_time(conn,"collection_status='collected'")
        last_processing=_last_time(conn,"processing_status='processed'")
        last_ai=_last_time(conn,"ai_status='processed'")
        last_advertio=_last_time(conn,"advertio_status='sent'")
        pending=conn.execute("SELECT COUNT(*) n FROM messages WHERE (collection_status='collected' AND processing_status='pending') OR (processing_status='processed' AND classification_status='pending') OR (processing_status='processed' AND ai_status='pending') OR (ai_status='processed' AND COALESCE(advertio_status,'waiting') IN ('waiting','retry'))").fetchone()["n"] or 0
        failed=conn.execute("SELECT COUNT(*) n FROM messages WHERE processing_status='failed' OR classification_status='failed' OR ai_status='failed' OR advertio_status='failed'").fetchone()["n"] or 0
        total=conn.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"] or 0
        if total==0: db_state="WARNING"; warning="Database has no collected messages yet."
        else: db_state="HEALTHY"; warning=""
        def activity(last,stage_pending,stage_failed):
            if stage_failed>0 and stage_pending==0: return "WARNING"
            if stage_pending>0: return "WARNING"
            return "HEALTHY" if last else "WARNING"
        processing_pending=conn.execute("SELECT COUNT(*) n FROM messages WHERE collection_status='collected' AND processing_status='pending'").fetchone()["n"] or 0
        classification_pending=conn.execute("SELECT COUNT(*) n FROM messages WHERE processing_status='processed' AND classification_status='pending'").fetchone()["n"] or 0
        ai_pending=conn.execute("SELECT COUNT(*) n FROM messages WHERE processing_status='processed' AND ai_status='pending'").fetchone()["n"] or 0
        advertio_pending=conn.execute("SELECT COUNT(*) n FROM messages WHERE ai_status='processed' AND COALESCE(advertio_status,'waiting') IN ('waiting','retry')").fetchone()["n"] or 0
        processing_failed=conn.execute("SELECT COUNT(*) n FROM messages WHERE processing_status='failed'").fetchone()["n"] or 0
        classification_failed=conn.execute("SELECT COUNT(*) n FROM messages WHERE classification_status='failed'").fetchone()["n"] or 0
        ai_failed=conn.execute("SELECT COUNT(*) n FROM messages WHERE ai_status='failed'").fetchone()["n"] or 0
        advertio_failed_count=conn.execute("SELECT COUNT(*) n FROM messages WHERE advertio_status='failed'").fetchone()["n"] or 0
        return {"crawler":"HEALTHY" if last_crawl else "WARNING","processing":activity(last_processing,processing_pending,processing_failed),"classification":activity(last_processing,classification_pending,classification_failed),"ai":activity(last_ai,ai_pending,ai_failed),"advertio":activity(last_advertio,advertio_pending,advertio_failed_count),"database":db_state,"last_crawl":last_crawl or "No crawl recorded","last_processing":last_processing or "No processing recorded","last_ai":last_ai or "No AI processing recorded","last_advertio":last_advertio or "No Advertio delivery recorded","backlog":int(pending),"failed":int(failed),"warning":warning}
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
def get_ai_pending_messages(limit=100,channel_username=None):
    return _get_messages("processing_status='processed' AND classification_status='processed' AND classification_category IN ('housinglist','transferlist','joblist') AND ai_status='pending'",[],limit,channel_username)
def get_advertio_pending_messages(limit=100,channel_username=None):
    conn=get_connection()
    try:
        sql="SELECT m.*,h.* FROM messages m INNER JOIN housinglist h ON h.processed_message_id=m.id WHERE m.processing_status='processed' AND m.ai_status='processed' AND m.ai_category='housinglist' AND COALESCE(m.advertio_status,'waiting') IN ('waiting','retry')"; values=[]
        if channel_username: sql += " AND m.channel_username=?"; values.append(channel_username)
        sql += " ORDER BY m.id LIMIT ?"; values.append(int(limit)); rows=conn.execute(sql,values).fetchall(); results=[]
        for row in rows:
            item=dict(row); item["housing_data"]={field:item.get(field) for field in CATEGORY_TABLES["housinglist"]}; results.append(item)
        return results
    finally: conn.close()
def get_previous_messages_by_sender(sender_id,before_id):
    if sender_id is None:return []
    conn=get_connection()
    try:return [dict(r) for r in conn.execute("SELECT id,message_id,channel_username,sender_id,raw_text,text FROM messages WHERE sender_id=? AND id<? AND COALESCE(raw_text,text,'')<>'' ORDER BY id",(sender_id,before_id)).fetchall()]
    finally: conn.close()
def update_message(message_id,channel_username,**fields):
    allowed={"cleaned_text","text","collection_status","processing_status","classification_status","classification_category","classification_error","classification_processed_at","classification_attempts","ai_status","pipeline_version","cleaned_at","ai_category","ai_processed_at","ai_error","advertio_status","advertio_lead_id","advertio_error","advertio_processed_at"}; updates={k:v for k,v in fields.items() if k in allowed}
    if not updates:return False
    assignments=", ".join(f"{k}=?" for k in updates); values=list(updates.values())+[channel_username,message_id]; conn=get_connection()
    try: cursor=conn.execute(f"UPDATE messages SET {assignments} WHERE channel_username=? AND message_id=?",values); conn.commit(); return cursor.rowcount>0
    finally: conn.close()
def delete_message(message_id,channel_username):
    conn=get_connection()
    try: cursor=conn.execute("DELETE FROM messages WHERE channel_username=? AND message_id=?",(channel_username,message_id)); conn.commit(); return cursor.rowcount>0
    finally: conn.close()
def update_processed_message(message_id,channel_username,**fields): return update_message(message_id,channel_username,**fields)
def save_category_record(processed_message_id,category,data):
    if category not in CATEGORY_TABLES: raise ValueError(f"Unsupported category: {category}")
    fields=CATEGORY_TABLES[category]; columns=["processed_message_id"]+list(fields); values=[processed_message_id]
    for field in fields:
        value=data.get(field)
        if field in {"features","skills"} and value is not None and not isinstance(value,str):value=json.dumps(value,ensure_ascii=False)
        if field in {"furnished","remote"} and value is not None:value=int(bool(value))
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
