"""Additive SQLite storage. Every claim and scheduling transition is transactional."""
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

from storage import database
from .models import stamp, utcnow
from . import recurrence
from .validation import ValidationError

SCHEMA = """
CREATE TABLE IF NOT EXISTS mm_messages (
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, content_json TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
 state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','archived')),
 created_by INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 import_namespace TEXT, import_key TEXT, UNIQUE(import_namespace,import_key));
CREATE TABLE IF NOT EXISTS mm_media (
 asset_key TEXT PRIMARY KEY, filename TEXT NOT NULL UNIQUE, original_name TEXT NOT NULL,
 kind TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
 file_id TEXT NOT NULL DEFAULT '', created_by INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mm_message_media (
 message_id INTEGER NOT NULL REFERENCES mm_messages(id), asset_key TEXT NOT NULL REFERENCES mm_media(asset_key),
 position INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(message_id,position));
CREATE TABLE IF NOT EXISTS mm_targets (
 target_id INTEGER PRIMARY KEY REFERENCES publishing_targets(id), eligible INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS mm_schedules (
 id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL REFERENCES mm_messages(id),
 spec_json TEXT NOT NULL, snapshot_json TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('active','paused','cancelled','completed')),
 next_due TEXT, next_index INTEGER NOT NULL DEFAULT 0, created_by INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mm_schedule_targets (
 schedule_id INTEGER NOT NULL REFERENCES mm_schedules(id), target_id INTEGER NOT NULL REFERENCES publishing_targets(id),
 chat_id TEXT NOT NULL, PRIMARY KEY(schedule_id,target_id));
CREATE TABLE IF NOT EXISTS mm_occurrences (
 id INTEGER PRIMARY KEY, schedule_id INTEGER NOT NULL REFERENCES mm_schedules(id), ordinal INTEGER NOT NULL,
 due_at TEXT NOT NULL, snapshot_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'waiting',
 UNIQUE(schedule_id,ordinal));
CREATE TABLE IF NOT EXISTS mm_deliveries (
 id INTEGER PRIMARY KEY, occurrence_id INTEGER NOT NULL REFERENCES mm_occurrences(id),
 target_id INTEGER NOT NULL REFERENCES publishing_targets(id), chat_id TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'waiting', attempts INTEGER NOT NULL DEFAULT 0,
 claim_token TEXT, lease_until TEXT, next_retry TEXT, telegram_message_id INTEGER, message_link TEXT,
 error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, UNIQUE(occurrence_id,target_id));
CREATE TABLE IF NOT EXISTS mm_delivery_attempts (
 id INTEGER PRIMARY KEY, delivery_id INTEGER NOT NULL REFERENCES mm_deliveries(id), number INTEGER NOT NULL,
 claim_token TEXT NOT NULL UNIQUE, started_at TEXT NOT NULL, finished_at TEXT,
 outcome TEXT NOT NULL DEFAULT 'sending', error TEXT NOT NULL DEFAULT '', telegram_message_id INTEGER);
CREATE TABLE IF NOT EXISTS mm_import_batches (
 id TEXT PRIMARY KEY, namespace TEXT NOT NULL, sha256 TEXT NOT NULL, preview_json TEXT NOT NULL,
 created_by INTEGER NOT NULL, created_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'preview',
 confirmed_at TEXT, activated_at TEXT);
CREATE TABLE IF NOT EXISTS mm_audit (
 id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, entity TEXT NOT NULL,
 details TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mm_runtime (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS mm_schedule_due ON mm_schedules(state,next_due);
CREATE INDEX IF NOT EXISTS mm_delivery_due ON mm_deliveries(status,next_retry);
CREATE INDEX IF NOT EXISTS mm_message_updated ON mm_messages(updated_at);
"""


class Repository:
    def __init__(self, connect=None):
        self.connect = connect or database.get_connection

    def initialize(self):
        with self.connection() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connection(self, write=False):
        conn = self.connect()
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def audit(conn, actor, action, entity, details=""):
        conn.execute("INSERT INTO mm_audit(actor,action,entity,details,created_at) VALUES(?,?,?,?,?)",
                     (str(actor), action, str(entity), str(details)[:4000], stamp()))
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_activity'").fetchone():
            conn.execute("INSERT INTO system_activity(kind,level,source,message,details,created_at) VALUES(?,?,?,?,?,?)",
                ("message_manager", "ERROR" if action in {"delivery_failed", "delivery_uncertain", "uncertain_recovery"} else "INFO",
                 "message_manager", action, json.dumps({"actor": str(actor), "entity": str(entity), "details": str(details)[:2000]}), stamp()))

    def targets(self):
        with self.connection() as c:
            return [dict(r) for r in c.execute("""SELECT t.*, COALESCE(m.eligible,0) AS mm_eligible
              FROM publishing_targets t LEFT JOIN mm_targets m ON m.target_id=t.id ORDER BY t.label""")]

    def eligible(self, target_id, enabled, actor):
        with self.connection(True) as c:
            c.execute("INSERT INTO mm_targets VALUES(?,?) ON CONFLICT(target_id) DO UPDATE SET eligible=excluded.eligible",
                      (int(target_id), int(enabled)))
            self.audit(c, actor, "target_eligibility", target_id, bool(enabled))

    def get_message(self, message_id):
        with self.connection() as c:
            row = c.execute("SELECT * FROM mm_messages WHERE id=?", (int(message_id),)).fetchone()
            if not row:
                raise ValidationError("Message not found")
            result = dict(row)
            result["content"] = json.loads(result["content_json"])
            return result

    def list_messages(self, query="", state="", target="", schedule="", date_from="", date_to="", limit=100, offset=0):
        sql, args = ["1=1"], []
        if query:
            sql.append("(m.title LIKE ? OR m.content_json LIKE ?)")
            args += [f"%{query[:200]}%"] * 2
        if state:
            sql.append("(m.state=? OR EXISTS(SELECT 1 FROM mm_schedules s JOIN mm_occurrences o ON o.schedule_id=s.id JOIN mm_deliveries d ON d.occurrence_id=o.id WHERE s.message_id=m.id AND d.status=?))")
            args += [state, state]
        if target:
            sql.append("EXISTS(SELECT 1 FROM mm_schedules s JOIN mm_schedule_targets t ON t.schedule_id=s.id WHERE s.message_id=m.id AND t.target_id=?)")
            args.append(int(target))
        if schedule:
            sql.append("EXISTS(SELECT 1 FROM mm_schedules s WHERE s.message_id=m.id AND s.state=?)")
            args.append(schedule)
        for value, op in ((date_from, ">="), (date_to, "<=")):
            if value:
                # Creation dates are filtered in UTC; delivery date filters are in the detail view.
                sql.append(f"substr(m.created_at,1,10){op}?")
                args.append(value)
        with self.connection() as c:
            return [dict(r) for r in c.execute("SELECT m.* FROM mm_messages m WHERE " + " AND ".join(sql) +
                  " ORDER BY m.id DESC LIMIT ? OFFSET ?", (*args, min(100, max(1, int(limit))), max(0, int(offset))))]

    def save_message(self, title, payload, actor, message_id=None, version=None, conn=None, namespace=None, key=None):
        if conn is None:
            with self.connection(True) as c:
                return self.save_message(title, payload, actor, message_id, version, c, namespace, key)
        title = str(title or "").strip()
        if not 1 <= len(title) <= 200:
            raise ValidationError("Title must contain 1–200 characters")
        encoded, now = json.dumps(payload, ensure_ascii=False), stamp()
        if message_id:
            row = conn.execute("UPDATE mm_messages SET title=?,content_json=?,version=version+1,updated_at=? WHERE id=? AND version=? AND state='draft'",
                               (title, encoded, now, int(message_id), int(version))).rowcount
            if not row:
                raise ValidationError("Draft changed or is archived; reload before editing")
            conn.execute("DELETE FROM mm_message_media WHERE message_id=?", (message_id,))
        else:
            message_id = conn.execute("""INSERT INTO mm_messages(title,content_json,created_by,created_at,updated_at,import_namespace,import_key)
                 VALUES(?,?,?,?,?,?,?)""", (title, encoded, actor, now, now, namespace, key)).lastrowid
        if payload.get("asset_key"):
            conn.execute("INSERT INTO mm_message_media VALUES(?,?,0)", (message_id, payload["asset_key"]))
        self.audit(conn, actor, "save_message", message_id)
        return message_id

    def archive(self, message_id, actor):
        with self.connection(True) as c:
            c.execute("UPDATE mm_messages SET state='archived',updated_at=? WHERE id=?", (stamp(), message_id))
            schedules = c.execute("SELECT id FROM mm_schedules WHERE message_id=? AND state IN ('active','paused')", (message_id,)).fetchall()
            for s in schedules:
                self._cancel(c, s["id"])
            self.audit(c, actor, "archive", message_id)

    def schedule(self, message_id, spec, targets, actor, conn=None):
        if conn is None:
            with self.connection(True) as c:
                return self.schedule(message_id, spec, targets, actor, c)
        row = conn.execute("SELECT * FROM mm_messages WHERE id=? AND state='draft'", (message_id,)).fetchone()
        if not row:
            raise ValidationError("An editable draft is required")
        snapshot = json.loads(row["content_json"])
        snapshot.update(title=row["title"], version=row["version"])
        due = recurrence.at(spec, 0)
        if due is None:
            raise ValidationError("Schedule has no occurrences")
        schedule_id = conn.execute("""INSERT INTO mm_schedules(message_id,spec_json,snapshot_json,state,next_due,created_by,created_at)
          VALUES(?,?,?,'active',?,?,?)""", (message_id, json.dumps(spec), json.dumps(snapshot), stamp(due), actor, stamp())).lastrowid
        for target in targets:
            # Recheck inside the activation transaction, including destination identity.
            current = conn.execute("SELECT t.chat_id,m.eligible FROM publishing_targets t JOIN mm_targets m ON m.target_id=t.id WHERE t.id=?", (target["id"],)).fetchone()
            if not current or not current["eligible"] or current["chat_id"] != target["chat_id"]:
                raise ValidationError("Destination eligibility changed; check again")
            conn.execute("INSERT INTO mm_schedule_targets VALUES(?,?,?)", (schedule_id, target["id"], target["chat_id"]))
        self.audit(conn, actor, "activate_schedule", schedule_id)
        return schedule_id

    @staticmethod
    def _cancel(c, schedule_id):
        c.execute("UPDATE mm_schedules SET state='cancelled',next_due=NULL WHERE id=?", (schedule_id,))
        c.execute("UPDATE mm_deliveries SET status='cancelled',updated_at=? WHERE status IN ('waiting','retry') AND occurrence_id IN (SELECT id FROM mm_occurrences WHERE schedule_id=?)", (stamp(), schedule_id))
        c.execute("UPDATE mm_occurrences SET state='cancelled' WHERE schedule_id=? AND state IN ('waiting','retry')", (schedule_id,))

    def schedule_action(self, schedule_id, action, actor):
        with self.connection(True) as c:
            row = c.execute("SELECT * FROM mm_schedules WHERE id=?", (schedule_id,)).fetchone()
            if not row:
                raise ValidationError("Schedule not found")
            if action == "cancel":
                self._cancel(c, schedule_id)
            elif action == "pause" and row["state"] == "active":
                c.execute("UPDATE mm_schedules SET state='paused' WHERE id=?", (schedule_id,))
            elif action == "resume" and row["state"] == "paused":
                c.execute("UPDATE mm_schedules SET state='active' WHERE id=?", (schedule_id,))
            else:
                raise ValidationError("Invalid schedule transition")
            self.audit(c, actor, action, schedule_id)

    def materialize(self, now=None, limit=25, once_grace=86400, recurring_grace=300):
        now = now or utcnow()
        with self.connection(True) as c:
            rows = c.execute("SELECT * FROM mm_schedules WHERE state='active' AND next_due<=? ORDER BY next_due LIMIT ?", (stamp(now), limit)).fetchall()
            for row in rows:
                spec = json.loads(row["spec_json"])
                due = datetime.fromisoformat(row["next_due"])
                grace = once_grace if spec["recurrence"] == "once" else recurring_grace
                state = "missed" if (now - due).total_seconds() > grace else "waiting"
                occurrence = c.execute("INSERT INTO mm_occurrences(schedule_id,ordinal,due_at,snapshot_json,state) VALUES(?,?,?,?,?)",
                    (row["id"], row["next_index"], stamp(due), row["snapshot_json"], state)).lastrowid
                c.execute("""INSERT INTO mm_deliveries(occurrence_id,target_id,chat_id,status,updated_at)
                    SELECT ?,target_id,chat_id,?,? FROM mm_schedule_targets WHERE schedule_id=?""", (occurrence, state, stamp(now), row["id"]))
                following, index = recurrence.next_after(spec, now)
                c.execute("UPDATE mm_schedules SET next_due=?,next_index=?,state=? WHERE id=?",
                          (stamp(following) if following else None, index, "active" if following else "completed", row["id"]))
                if state == "missed":
                    self.audit(c, "worker", "missed", occurrence)
            return len(rows)

    def recover(self, now=None):
        now = now or utcnow()
        with self.connection(True) as c:
            rows = c.execute("SELECT id,claim_token FROM mm_deliveries WHERE status='sending' AND lease_until<=?", (stamp(now),)).fetchall()
            for row in rows:
                error = "Worker lease expired after claim; verify Telegram before manual resend"
                c.execute("UPDATE mm_deliveries SET status='uncertain',error=?,updated_at=? WHERE id=?", (error, stamp(now), row["id"]))
                c.execute("UPDATE mm_delivery_attempts SET outcome='uncertain',error=?,finished_at=? WHERE claim_token=? AND finished_at IS NULL", (error, stamp(now), row["claim_token"]))
                self._aggregate(c, row["id"])
                self.audit(c, "worker", "uncertain_recovery", row["id"])
            return len(rows)

    def claim(self, now=None, spacing=1.1, lease_seconds=300, once_grace=86400, recurring_grace=300):
        now = now or utcnow()
        with self.connection(True) as c:
            if c.execute("SELECT 1 FROM publishing_targets WHERE rate_limited_until>? LIMIT 1", (stamp(now),)).fetchone():
                return None
            bot_limit = c.execute("SELECT value FROM mm_runtime WHERE key='bot_cooldown'").fetchone()
            if bot_limit and bot_limit[0] > stamp(now):
                return None
            slot = c.execute("SELECT value FROM mm_runtime WHERE key='next_send' ").fetchone()
            if slot and slot[0] > stamp(now):
                return None
            rows = c.execute("""SELECT d.*,o.snapshot_json,o.due_at,s.spec_json FROM mm_deliveries d
              JOIN mm_occurrences o ON o.id=d.occurrence_id JOIN mm_schedules s ON s.id=o.schedule_id
              WHERE d.status IN ('waiting','retry') AND (d.next_retry IS NULL OR d.next_retry<=?)
                AND s.state IN ('active','completed') ORDER BY o.due_at,d.id LIMIT 100""", (stamp(now),)).fetchall()
            for row in rows:
                spec = json.loads(row["spec_json"])
                grace = once_grace if spec["recurrence"] == "once" else recurring_grace
                # Already-attempted, definite retries follow their persisted backoff even after the grace window.
                if not row["attempts"] and (now - datetime.fromisoformat(row["due_at"])).total_seconds() > grace:
                    c.execute("UPDATE mm_deliveries SET status='missed',updated_at=? WHERE id=?", (stamp(now), row["id"]))
                    self._aggregate(c, row["id"])
                    continue
                target_slot = c.execute("SELECT value FROM mm_runtime WHERE key=?", (f"target:{row['target_id']}",)).fetchone()
                if target_slot and target_slot[0] > stamp(now):
                    continue
                token = uuid.uuid4().hex
                c.execute("UPDATE mm_deliveries SET status='sending',attempts=attempts+1,claim_token=?,lease_until=?,updated_at=? WHERE id=?",
                          (token, stamp(now + timedelta(seconds=lease_seconds)), stamp(now), row["id"]))
                c.execute("INSERT INTO mm_delivery_attempts(delivery_id,number,claim_token,started_at) VALUES(?,?,?,?)",
                          (row["id"], row["attempts"] + 1, token, stamp(now)))
                for key, delay in (("next_send", spacing), (f"target:{row['target_id']}", 3)):
                    c.execute("INSERT INTO mm_runtime VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, stamp(now + timedelta(seconds=delay))))
                c.execute("UPDATE mm_occurrences SET state='sending' WHERE id=?", (row["occurrence_id"],))
                result = dict(row)
                result.update(claim_token=token, attempts=row["attempts"] + 1, content=json.loads(row["snapshot_json"]))
                return result
            return None

    @staticmethod
    def _aggregate(c, delivery_id):
        oid = c.execute("SELECT occurrence_id FROM mm_deliveries WHERE id=?", (delivery_id,)).fetchone()[0]
        statuses = {r[0] for r in c.execute("SELECT status FROM mm_deliveries WHERE occurrence_id=?", (oid,))}
        state = next((s for s in ("sending", "retry", "waiting", "uncertain", "failed", "missed", "cancelled") if s in statuses), "sent")
        c.execute("UPDATE mm_occurrences SET state=? WHERE id=?", (state, oid))

    def cooldown(self, seconds, now=None):
        now = now or utcnow()
        until = stamp(now + timedelta(seconds=seconds + 1))
        with self.connection(True) as c:
            c.execute("UPDATE publishing_targets SET rate_limited_until=MAX(COALESCE(rate_limited_until,''),?)", (until,))
            c.execute("INSERT INTO mm_runtime VALUES('bot_cooldown',?) ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)", (until,))

    def finish(self, delivery, result, max_attempts=3, now=None, backoff=30):
        now = now or utcnow()
        status = result.status
        retry_at = None
        if status == "retry":
            if delivery["attempts"] >= max_attempts:
                status = "failed"
            else:
                retry_at = stamp(now + timedelta(seconds=max(result.retry_after, backoff * 2 ** (delivery["attempts"] - 1))))
        with self.connection(True) as c:
            changed = c.execute("""UPDATE mm_deliveries SET status=?,next_retry=?,telegram_message_id=?,message_link=?,error=?,updated_at=?
              WHERE id=? AND status='sending' AND claim_token=?""",
              (status, retry_at, result.message_id, result.message_link, result.error[:2000], stamp(now), delivery["id"], delivery["claim_token"])).rowcount
            if not changed:
                return False
            c.execute("UPDATE mm_delivery_attempts SET outcome=?,error=?,telegram_message_id=?,finished_at=? WHERE claim_token=? AND finished_at IS NULL",
                      (status, result.error[:2000], result.message_id, stamp(now), delivery["claim_token"]))
            if result.retry_after:
                until = stamp(now + timedelta(seconds=result.retry_after + 1))
                c.execute("UPDATE publishing_targets SET rate_limited_until=MAX(COALESCE(rate_limited_until,''),?)", (until,))
                c.execute("INSERT INTO mm_runtime VALUES('bot_cooldown',?) ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)", (until,))
            if result.file_id and delivery["content"].get("asset_key"):
                c.execute("UPDATE mm_media SET file_id=? WHERE asset_key=?", (result.file_id, delivery["content"]["asset_key"]))
            self._aggregate(c, delivery["id"])
            self.audit(c, "worker", "delivery_" + status, delivery["id"], result.error)
            return True

    def manual_retry(self, delivery_id, actor, confirmed=False):
        if not confirmed:
            raise ValidationError("Confirm duplicate risk before resending")
        with self.connection(True) as c:
            row = c.execute("SELECT d.*,s.state AS schedule_state FROM mm_deliveries d JOIN mm_occurrences o ON o.id=d.occurrence_id JOIN mm_schedules s ON s.id=o.schedule_id WHERE d.id=?", (delivery_id,)).fetchone()
            if not row or row["status"] not in {"uncertain", "failed"} or row["schedule_state"] in {"cancelled", "paused"}:
                raise ValidationError("Only failed/uncertain deliveries of active/completed schedules may be retried")
            # attempts remains monotonic and the append-only history is retained. Manual retry gets one attempt.
            c.execute("UPDATE mm_deliveries SET status='retry',next_retry=?,updated_at=? WHERE id=?", (stamp(), stamp(), delivery_id))
            self._aggregate(c, delivery_id)
            self.audit(c, actor, "manual_resend_duplicate_risk_confirmed", delivery_id)

    def details(self, message_id):
        with self.connection() as c:
            schedules = [dict(r) for r in c.execute("SELECT * FROM mm_schedules WHERE message_id=? ORDER BY id DESC", (message_id,))]
            deliveries = [dict(r) for r in c.execute("""SELECT d.*,o.due_at,o.state AS occurrence_state,o.ordinal,s.id AS schedule_id
             FROM mm_deliveries d JOIN mm_occurrences o ON o.id=d.occurrence_id JOIN mm_schedules s ON s.id=o.schedule_id
             WHERE s.message_id=? ORDER BY d.id DESC LIMIT 200""", (message_id,))]
            attempts = [dict(r) for r in c.execute("""SELECT a.* FROM mm_delivery_attempts a JOIN mm_deliveries d ON d.id=a.delivery_id
              JOIN mm_occurrences o ON o.id=d.occurrence_id JOIN mm_schedules s ON s.id=o.schedule_id
              WHERE s.message_id=? ORDER BY a.id DESC LIMIT 500""", (message_id,))]
        return schedules, deliveries, attempts

    def media(self, key):
        with self.connection() as c:
            row = c.execute("SELECT * FROM mm_media WHERE asset_key=?", (key,)).fetchone()
            if not row:
                raise ValidationError("Unknown media asset key")
            return dict(row)

    def worker_heartbeat(self, state='running', error=''):
        with self.connection(True) as c:
            for key, value in [('worker_heartbeat', stamp()), ('worker_state', state), ('worker_error', error[:500])]:
                c.execute('INSERT INTO mm_runtime VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

    def dashboard(self, now=None):
        now = now or utcnow()
        with self.connection() as c:
            counts = {r['status']: r['n'] for r in c.execute('SELECT status,COUNT(*) AS n FROM mm_deliveries GROUP BY status')}
            upcoming = c.execute("""SELECT COUNT(DISTINCT message_id) FROM (
             SELECT message_id FROM mm_schedules WHERE state='active' AND next_due IS NOT NULL
             UNION SELECT s.message_id FROM mm_schedules s JOIN mm_occurrences o ON o.schedule_id=s.id
             JOIN mm_deliveries d ON d.occurrence_id=o.id WHERE s.state IN ('active','completed') AND d.status IN ('waiting','sending','retry'))""").fetchone()[0]
            runtime = dict(c.execute("SELECT key,value FROM mm_runtime WHERE key LIKE 'worker_%' OR key='bot_cooldown'"))
            shared = c.execute('SELECT MAX(rate_limited_until) FROM publishing_targets').fetchone()[0]
        heartbeat = runtime.get('worker_heartbeat')
        fresh = bool(heartbeat and (now - datetime.fromisoformat(heartbeat)).total_seconds() < 360)
        state = runtime.get('worker_state', 'unknown') if fresh else 'stale' if heartbeat else 'unknown'
        cooldown = max(shared or '', runtime.get('bot_cooldown', ''))
        return dict(upcoming=upcoming, sent=counts.get('sent',0), failed=counts.get('failed',0), uncertain=counts.get('uncertain',0),
            worker_state=state, heartbeat=heartbeat, worker_error=runtime.get('worker_error',''),
            cooldown=cooldown if cooldown and cooldown > stamp(now) else None)
