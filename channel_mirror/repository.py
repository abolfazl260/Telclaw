"""Durable mirror configuration and queue, isolated from all other publishers."""

from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import sqlite3
import uuid

from storage import database

from .models import SourcePost, SendOutcome, ValidationError, caption, required, stamp, telegram_id
from .schema import STATEMENTS
from .crawl_repository import CrawlRepository
from .transport_repository import TransportRepository
from .admin import AdminRepository, redact


class Repository(CrawlRepository, TransportRepository, AdminRepository):
    def __init__(self, connect=None, *, busy_timeout_ms=10000):
        self.connect = connect or database.get_connection
        self.busy_timeout_ms = max(0, int(busy_timeout_ms))

    @contextmanager
    def connection(self, write=False):
        conn = self.connect()
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        """Startup only. No legacy migrations, recovery, or data scans here."""
        with self.connection(True) as conn:
            for sql in STATEMENTS:
                conn.execute(sql)
            conn.execute("CREATE TABLE IF NOT EXISTS cm_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(cm_source_items)")}
            if "media_metadata" not in columns:
                conn.execute("ALTER TABLE cm_source_items ADD COLUMN media_metadata TEXT NOT NULL DEFAULT '{}'")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(cm_deliveries)")}
            for name, definition in {"publish_stage": "TEXT NOT NULL DEFAULT 'new'", "media_sent_at": "TEXT", "caption_set_at": "TEXT"}.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE cm_deliveries ADD COLUMN {name} {definition}")

    @staticmethod
    def _audit(conn, actor, action, entity_id, details, now):
        conn.execute("""INSERT INTO cm_audit(actor,action,entity_id,details,created_at)
                        VALUES(?,?,?,?,?)""",
                     (required(str(actor) if actor is not None else "", "Actor"), action,
                      entity_id, redact(json.dumps(details, ensure_ascii=False)), now))

    @staticmethod
    def _rule(conn, rule_id):
        row = conn.execute("SELECT * FROM cm_rules WHERE id=?", (rule_id,)).fetchone()
        if not row:
            raise ValidationError("Mirror rule not found")
        return dict(row)

    def create_rule(self, source_peer_id, source_name, destination_chat_id,
                    destination_username, footer, actor, *, mode="media_only", now=None):
        telegram_id(source_peer_id)
        telegram_id(destination_chat_id)
        source_name = required(source_name, "Resolvable source name")
        destination_username = required(destination_username, "Public destination @username")
        caption(destination_username, footer)
        if mode != "media_only":
            raise ValidationError("V1 mode must be media_only")
        ts = stamp(now)
        with self.connection(True) as conn:
            try:
                rid = conn.execute("""INSERT INTO cm_rules(source_peer_id,source_name,
                    destination_chat_id,destination_username,footer,created_by,updated_by,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (source_peer_id, source_name, destination_chat_id,
                    destination_username, footer, str(actor), str(actor), ts, ts)).lastrowid
            except sqlite3.IntegrityError as exc:
                raise ValidationError("A rule already exists for this source and destination") from exc
            self._audit(conn, actor, "create_rule", rid, self._rule(conn, rid), ts)
            return rid

    def update_rule(self, rule_id, actor, *, source_name=None, destination_username=None,
                    footer=None, now=None):
        """Peer/chat IDs and mode are immutable; existing captions are snapshots."""
        ts = stamp(now)
        with self.connection(True) as conn:
            before = self._rule(conn, rule_id)
            name = required(source_name if source_name is not None else before["source_name"],
                            "Resolvable source name")
            username = required(destination_username if destination_username is not None
                                else before["destination_username"], "Public destination @username")
            text = footer if footer is not None else before["footer"]
            caption(username, text)
            conn.execute("""UPDATE cm_rules SET source_name=?,destination_username=?,footer=?,
                updated_by=?,updated_at=? WHERE id=?""", (name, username, text, str(actor), ts, rule_id))
            self._audit(conn, actor, "update_rule", rule_id,
                        {"before": before, "after": self._rule(conn, rule_id)}, ts)

    def set_rule_state(self, rule_id, state, actor, *, now=None):
        if state not in {"active", "disabled", "paused"}:
            raise ValidationError("Invalid rule state")
        ts = stamp(now)
        with self.connection(True) as conn:
            before = self._rule(conn, rule_id)
            if before["state"] == state:
                return
            if state == "paused" and before["state"] != "active":
                raise ValidationError("Only an active rule may be paused")
            watermark = before["activation_watermark"]
            if state == "active":
                # Resume/reactivation must not backfill the paused/disabled gap.
                watermark = max(ts, before["created_at"], watermark or "")
            conn.execute("""UPDATE cm_rules SET state=?,activation_watermark=?,updated_by=?,updated_at=?
                WHERE id=?""", (state, watermark, str(actor), ts, rule_id))
            if state == "disabled":
                conn.execute("""UPDATE cm_deliveries SET status='skipped',error='Rule disabled',
                    next_retry=NULL,updated_at=?,finished_at=?
                    WHERE rule_id=? AND status IN ('waiting','retry')""", (ts, ts, rule_id))
            self._audit(conn, actor, "rule_" + state, rule_id,
                        {"before": before, "after": self._rule(conn, rule_id)}, ts)

    def get_rule(self, rule_id):
        with self.connection() as conn:
            return self._rule(conn, rule_id)

    def list_rules(self):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM cm_rules ORDER BY id")]

    def enqueue(self, post: SourcePost, *, now=None) -> list[int]:
        """Record a complete media unit and fan out atomically to active rules.

        The first observation owns immutable source metadata/order. Repeated
        scans may have a different crawl ID but cannot change album membership.
        """
        if not isinstance(post, SourcePost):
            raise ValidationError("A complete SourcePost is required")
        ts = stamp(now)
        with self.connection(True) as conn:
            return self._enqueue(conn, post, ts)

    def _enqueue(self, conn, post, ts, rule_ids=None):
        row = conn.execute("SELECT id FROM cm_posts WHERE source_peer_id=? AND identity=?",
                           (post.source_peer_id, post.identity)).fetchone()
        if row:
            post_id = row["id"]
            existing = conn.execute("SELECT message_id FROM cm_source_items WHERE post_id=? ORDER BY position",
                                    (post_id,)).fetchall()
            if [r[0] for r in existing] != [item.message_id for item in post.items]:
                raise ValidationError("Complete album membership changed; refusing partial or edited album")
        else:
            post_id = conn.execute("""INSERT INTO cm_posts(source_peer_id,identity,grouped_id,
                crawl_run_id,created_at) VALUES(?,?,?,?,?)""", (post.source_peer_id, post.identity,
                post.items[0].grouped_id, post.crawl_run_id, ts)).lastrowid
            try:
                conn.executemany("""INSERT INTO cm_source_items(post_id,source_peer_id,message_id,
                    message_date,grouped_id,media_kind,position,crawl_run_id,media_metadata) VALUES(?,?,?,?,?,?,?,?,?)""",
                    [(post_id, post.source_peer_id, item.message_id, stamp(item.date), item.grouped_id,
                      item.media_kind, position, post.crawl_run_id, json.dumps(item.media_metadata)) for position, item in enumerate(post.items)])
            except sqlite3.IntegrityError as exc:
                raise ValidationError("A source message already belongs to a different post") from exc
        # Use persisted source dates, never dates supplied by a later scan.
        oldest = conn.execute("SELECT MIN(message_date) FROM cm_source_items WHERE post_id=?",
                              (post_id,)).fetchone()[0]
        rules = conn.execute("""SELECT * FROM cm_rules WHERE source_peer_id=? AND state='active'
                                AND activation_watermark<? ORDER BY id""",
                             (post.source_peer_id, oldest)).fetchall()
        created = []
        for rule in rules:
            if rule_ids is not None and rule["id"] not in rule_ids:
                continue
            cursor = conn.execute("""INSERT INTO cm_deliveries(post_id,rule_id,destination_chat_id,
                destination_username,caption,created_at,updated_at) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(post_id,destination_chat_id) DO NOTHING""",
                (post_id, rule["id"], rule["destination_chat_id"], rule["destination_username"],
                 caption(rule["destination_username"], rule["footer"]), ts, ts))
            if cursor.rowcount:
                created.append(cursor.lastrowid)
        return created

    @staticmethod
    def _delivery(conn, delivery_id):
        row = conn.execute("""SELECT d.*,p.source_peer_id,p.identity,p.grouped_id,p.crawl_run_id
            FROM cm_deliveries d JOIN cm_posts p ON p.id=d.post_id WHERE d.id=?""", (delivery_id,)).fetchone()
        if not row:
            raise ValidationError("Mirror delivery not found")
        result = dict(row)
        result["destination_message_ids"] = json.loads(result["destination_message_ids"])
        result["items"] = [dict(r) for r in conn.execute(
            "SELECT * FROM cm_source_items WHERE post_id=? ORDER BY position", (row["post_id"],))]
        for item in result["items"]:
            item["media_metadata"] = json.loads(item["media_metadata"])
        return result

    def get_delivery(self, delivery_id):
        with self.connection() as conn:
            return self._delivery(conn, delivery_id)

    def list_deliveries(self, status=None, *, limit=100):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("""SELECT * FROM cm_deliveries
                WHERE (? IS NULL OR status=?) ORDER BY id DESC LIMIT ?""",
                (status, status, min(1000, max(1, int(limit)))))]

    def attempts(self, delivery_id):
        with self.connection() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM cm_delivery_attempts WHERE delivery_id=? ORDER BY number", (delivery_id,))]
            for row in rows:
                row["destination_message_ids"] = json.loads(row["destination_message_ids"])
            return rows

    def claim(self, *, now=None, lease_seconds=300, source_peer_id=None):
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValidationError("Lease duration must be positive seconds")
        ts = stamp(now)
        lease = stamp(datetime.fromisoformat(ts) + timedelta(seconds=lease_seconds))
        with self.connection(True) as conn:
            if not self.enabled(conn):
                return None
            row = conn.execute("""SELECT d.id FROM cm_deliveries d JOIN cm_rules r ON r.id=d.rule_id
                WHERE r.state='active' AND d.status IN ('waiting','retry') AND d.attempts<d.attempt_limit
                AND (? IS NULL OR r.source_peer_id=?)
                AND (d.next_retry IS NULL OR d.next_retry<=?) ORDER BY d.id LIMIT 1""", (source_peer_id, source_peer_id, ts)).fetchone()
            if not row:
                return None
            token = uuid.uuid4().hex
            conn.execute("""UPDATE cm_deliveries SET status='sending',attempts=attempts+1,
                claim_token=?,lease_until=?,next_retry=NULL,updated_at=?,finished_at=NULL WHERE id=?""",
                         (token, lease, ts, row["id"]))
            delivery = self._delivery(conn, row["id"])
            conn.execute("""INSERT INTO cm_delivery_attempts(delivery_id,number,claim_token,started_at)
                VALUES(?,?,?,?)""", (row["id"], delivery["attempts"], token, ts))
            return delivery

    def finish(self, delivery_id, claim_token, result: SendOutcome, *, now=None):
        if not isinstance(result, SendOutcome):
            raise ValidationError("A SendOutcome is required")
        ts = stamp(now)
        with self.connection(True) as conn:
            row = conn.execute("""SELECT d.*,r.state AS rule_state FROM cm_deliveries d
                JOIN cm_rules r ON r.id=d.rule_id WHERE d.id=? AND d.status='sending'
                AND d.claim_token=? AND d.lease_until>?""", (delivery_id, claim_token, ts)).fetchone()
            if not row:
                return False
            size = conn.execute("SELECT COUNT(*) FROM cm_source_items WHERE post_id=?", (row["post_id"],)).fetchone()[0]
            status, error, retry_at = result.status, result.error, None
            if (status == "sent" and len(result.message_ids) != size) or (status != "sent" and result.message_ids):
                status, error = "uncertain", "Incomplete/ambiguous destination result. " + error
            if status == "retry":
                if row["rule_state"] == "disabled":
                    status, error = "skipped", "Rule disabled. " + error
                elif row["attempts"] >= row["attempt_limit"]:
                    status = "failed"
                else:
                    delay = max(result.retry_after, min(3600, 30 * 2 ** min(row["attempts"] - 1, 7)))
                    retry_at = stamp(datetime.fromisoformat(ts) + timedelta(seconds=delay))
            # A successful album copy is durable even while its caption needs
            # retry/review. Empty outcome IDs must never erase that checkpoint.
            known = json.loads(row["destination_message_ids"]) if row["publish_stage"] in {"caption_pending", "partial"} else []
            ids = json.dumps(result.message_ids or known)
            conn.execute("""UPDATE cm_deliveries SET status=?,next_retry=?,destination_message_ids=?,
                error=?,updated_at=?,finished_at=?,claim_token=NULL,lease_until=NULL WHERE id=?""",
                (status, retry_at, ids, error[:2000], ts, None if status == "retry" else ts, delivery_id))
            if status == "sent":
                conn.execute("""UPDATE cm_deliveries SET publish_stage='complete',
                    media_sent_at=COALESCE(media_sent_at,?),caption_set_at=? WHERE id=?""", (ts, ts, delivery_id))
            conn.execute("""UPDATE cm_delivery_attempts SET outcome=?,error=?,destination_message_ids=?,
                finished_at=? WHERE claim_token=? AND finished_at IS NULL""",
                (status, error[:2000], ids, ts, claim_token))
            self._audit(conn, "worker", "delivery_" + status, delivery_id, {"error": error[:2000]}, ts)
            return True

    def recover_expired(self, *, now=None):
        """Expired claims may already have sent: quarantine, never auto-retry."""
        ts = stamp(now)
        error = "Worker lease expired; inspect Telegram before deciding whether to retry"
        with self.connection(True) as conn:
            rows = conn.execute("SELECT id,claim_token FROM cm_deliveries WHERE status='sending' AND lease_until<=?",
                                (ts,)).fetchall()
            for row in rows:
                conn.execute("""UPDATE cm_deliveries SET status='uncertain',error=?,updated_at=?,finished_at=?,
                    claim_token=NULL,lease_until=NULL WHERE id=?""", (error, ts, ts, row["id"]))
                conn.execute("""UPDATE cm_delivery_attempts SET outcome='uncertain',error=?,finished_at=?
                    WHERE claim_token=? AND finished_at IS NULL""", (error, ts, row["claim_token"]))
                self._audit(conn, "worker", "lease_expired", row["id"], {"error": error}, ts)
            return len(rows)

    def review(self, delivery_id, action, actor, reason, *, message_ids=(),
               confirm_duplicate_risk=False, now=None):
        """Explicit admin resolution; retries require acknowledging duplicates."""
        reason = required(reason, "Review reason")
        if action not in {"sent", "retry", "skipped"}:
            raise ValidationError("Invalid review action")
        outcome = SendOutcome(action, tuple(message_ids))
        if action == "retry" and confirm_duplicate_risk is not True:
            raise ValidationError("Confirm duplicate risk before retrying")
        if action != "sent" and outcome.message_ids:
            raise ValidationError("Destination IDs are only accepted when confirming sent")
        ts = stamp(now)
        with self.connection(True) as conn:
            row = self._delivery(conn, delivery_id)
            if row["status"] not in {"uncertain", "failed"}:
                raise ValidationError("Only uncertain/failed deliveries may be reviewed")
            if action == "sent" and len(outcome.message_ids) != len(row["items"]):
                raise ValidationError("All destination message IDs are required")
            rule = self._rule(conn, row["rule_id"])
            if action == "retry" and rule["state"] != "active":
                raise ValidationError("Activate the rule before retrying")
            # Historical attempt results stay immutable, including any partial IDs.
            ids = list(outcome.message_ids) if action == "sent" else row["destination_message_ids"]
            conn.execute("""UPDATE cm_deliveries SET status=?,destination_message_ids=?,error=?,
                next_retry=?,updated_at=?,finished_at=?,attempt_limit=MAX(attempt_limit,attempts+1)
                WHERE id=?""", (action, json.dumps(ids), reason[:2000], ts if action == "retry" else None,
                ts, None if action == "retry" else ts, delivery_id))
            if action == "sent":
                conn.execute("""UPDATE cm_deliveries SET publish_stage='complete',
                    media_sent_at=COALESCE(media_sent_at,?),caption_set_at=? WHERE id=?""", (ts, ts, delivery_id))
            self._audit(conn, actor, "review_" + action, delivery_id,
                        {"previous_status": row["status"], "reason": reason,
                         "message_ids": ids, "duplicate_risk_confirmed": confirm_duplicate_risk}, ts)

    def audit_log(self, *, limit=100):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM cm_audit ORDER BY id DESC LIMIT ?", (min(1000, max(1, int(limit))),))]
