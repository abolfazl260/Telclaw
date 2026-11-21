"""Transactional crawl staging and post-crawl dispatch outbox; no transport."""

from datetime import datetime, timedelta
import json
import uuid

from .models import SourceItem, SourcePost, ValidationError, required, stamp, telegram_id

CRAWL_LEASE_SECONDS = 600


def selector(value):
    return str(value).strip().lstrip("@").casefold()


def lease(ts):
    return stamp(datetime.fromisoformat(ts) + timedelta(seconds=CRAWL_LEASE_SECONDS))


class CrawlRepository:
    """Methods mixed into Repository to share its connection/queue transaction."""

    def has_active_rules(self):
        with self.connection() as conn:
            return self.enabled(conn) and conn.execute("SELECT 1 FROM cm_rules WHERE state='active' LIMIT 1").fetchone() is not None

    def begin_crawl(self, source_name, from_date, to_date, crawl_mode, *, now=None):
        ts, run_id = stamp(now), uuid.uuid4().hex
        with self.connection(True) as conn:
            conn.execute("""INSERT INTO cm_crawl_runs(id,source_name,from_date,to_date,crawl_mode,
                started_at,updated_at,lease_until) VALUES(?,?,?,?,?,?,?,?)""",
                (run_id, selector(required(source_name, "Source name")), str(from_date), str(to_date),
                 crawl_mode, ts, ts, lease(ts)))
        return run_id

    @staticmethod
    def _running(conn, run_id, ts):
        row = conn.execute("SELECT * FROM cm_crawl_runs WHERE id=? AND state='running' AND lease_until>?",
                           (run_id, ts)).fetchone()
        if not row:
            raise ValidationError("Crawl run is no longer active")
        return row

    def bind_crawl(self, run_id, source_peer_id, *, copy_allowed=True, now=None):
        telegram_id(source_peer_id)
        ts = stamp(now)
        with self.connection(True) as conn:
            self._running(conn, run_id, ts)
            rules = [dict(r) for r in conn.execute("""SELECT id,activation_watermark FROM cm_rules
                        WHERE source_peer_id=? AND state='active' ORDER BY id""", (source_peer_id,))]
            if not copy_allowed:
                rules = []
            conn.execute("""UPDATE cm_crawl_runs SET source_peer_id=?,rules_snapshot=?,updated_at=?,lease_until=?
                            WHERE id=?""", (source_peer_id, json.dumps(rules), ts, lease(ts), run_id))
            return rules

    def touch_crawl(self, run_id, *, now=None):
        ts = stamp(now)
        with self.connection(True) as conn:
            self._running(conn, run_id, ts)
            conn.execute("UPDATE cm_crawl_runs SET updated_at=?,lease_until=? WHERE id=?", (ts, lease(ts), run_id))

    @staticmethod
    def _stage(conn, run_id, item, complete=False):
        conn.execute("""INSERT INTO cm_crawl_items(run_id,message_id,message_date,grouped_id,media_kind,
            media_metadata,album_complete) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(run_id,message_id) DO NOTHING""",
            (run_id, item.message_id, stamp(item.date), item.grouped_id, item.media_kind,
             json.dumps(item.media_metadata), int(complete)))

    def stage_crawl_item(self, run_id, item: SourceItem, *, now=None):
        ts = stamp(now)
        with self.connection(True) as conn:
            self._running(conn, run_id, ts)
            self._stage(conn, run_id, item)
            conn.execute("UPDATE cm_crawl_runs SET updated_at=?,lease_until=? WHERE id=?", (ts, lease(ts), run_id))

    def crawl_groups(self, run_id):
        with self.connection() as conn:
            return [dict(r) for r in conn.execute("""SELECT grouped_id,MIN(message_id) AS first_id,
                MAX(message_id) AS last_id FROM cm_crawl_items WHERE run_id=? AND grouped_id IS NOT NULL
                GROUP BY grouped_id ORDER BY MIN(message_id)""", (run_id,))]

    def replace_crawl_album(self, run_id, grouped_id, items, *, now=None):
        ts = stamp(now)
        with self.connection(True) as conn:
            row = self._running(conn, run_id, ts)
            if items:
                post = SourcePost(row["source_peer_id"], tuple(items), run_id, album_complete=True)
                if post.items[0].grouped_id != grouped_id:
                    raise ValidationError("Refetched album group differs from seed")
            conn.execute("DELETE FROM cm_crawl_items WHERE run_id=? AND grouped_id=?", (run_id, grouped_id))
            for item in items:
                self._stage(conn, run_id, item, complete=True)
            conn.execute("UPDATE cm_crawl_runs SET updated_at=?,lease_until=? WHERE id=?", (ts, lease(ts), run_id))

    def complete_crawl(self, run_id, *, now=None):
        """Release ALL staged posts and persist the trigger in one transaction."""
        ts = stamp(now)
        with self.connection(True) as conn:
            row = self._running(conn, run_id, ts)
            snapshot = json.loads(row["rules_snapshot"])
            # A mid-crawl activation/resume is a new generation, not this run's opt-in.
            rules = {r["id"] for r in snapshot if conn.execute("""SELECT 1 FROM cm_rules
                WHERE id=? AND state='active' AND activation_watermark=?""",
                (r["id"], r["activation_watermark"])).fetchone()}
            groups = {}
            for item in conn.execute("SELECT * FROM cm_crawl_items WHERE run_id=? ORDER BY message_id", (run_id,)):
                gid = item["grouped_id"]
                if gid is not None and not item["album_complete"]:
                    raise ValidationError("Unverified album cannot be released")
                key = ("album", gid) if gid is not None else ("message", item["message_id"])
                groups.setdefault(key, []).append(SourceItem(item["message_id"], datetime.fromisoformat(item["message_date"]),
                    item["media_kind"], gid, json.loads(item["media_metadata"])))
            count = 0
            if rules and self.enabled(conn):
                for (kind, _), items in groups.items():
                    count += len(self._enqueue(conn, SourcePost(row["source_peer_id"], tuple(items), run_id,
                                              album_complete=kind == "album"), ts, rule_ids=rules))
            conn.execute("""UPDATE cm_crawl_runs SET state='completed',updated_at=?,finished_at=?,
                lease_until=NULL,deliveries_created=? WHERE id=?""", (ts, ts, count, run_id))
            event = None
            if rules:
                eid = conn.execute("""INSERT INTO cm_dispatch_events(run_id,source_peer_id,deliveries_created,created_at)
                    VALUES(?,?,?,?)""", (run_id, row["source_peer_id"], count, ts)).lastrowid
                event = dict(conn.execute("SELECT * FROM cm_dispatch_events WHERE id=?", (eid,)).fetchone())
            return {"run_id": run_id, "state": "completed", "deliveries_created": count, "dispatch": event}

    def abort_crawl(self, run_id, state, error="", *, now=None):
        if state not in {"failed", "stopped", "interrupted", "mirror_failed"}:
            raise ValidationError("Invalid crawl failure state")
        ts = stamp(now)
        with self.connection(True) as conn:
            conn.execute("""UPDATE cm_crawl_runs SET state=?,error=?,updated_at=?,finished_at=?,lease_until=NULL
                WHERE id=? AND state='running'""", (state, str(error)[:2000], ts, ts, run_id))

    def recover_crawls(self, *, now=None):
        ts = stamp(now)
        with self.connection(True) as conn:
            return conn.execute("""UPDATE cm_crawl_runs SET state='interrupted',error='Crawl lease expired',
                updated_at=?,finished_at=?,lease_until=NULL WHERE state='running' AND lease_until<=?""",
                (ts, ts, ts)).rowcount

    def get_crawl(self, run_id):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM cm_crawl_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise ValidationError("Crawl run not found")
            return dict(row)

    def pending_dispatches(self, limit=100, source_peer_id=None):
        with self.connection() as conn:
            return [dict(r) for r in conn.execute("""SELECT * FROM cm_dispatch_events WHERE acknowledged_at IS NULL
                    AND (? IS NULL OR source_peer_id=?) ORDER BY id LIMIT ?""",
                    (source_peer_id, source_peer_id, min(1000, max(1, int(limit)))))]

    def acknowledge_dispatch(self, event_id, *, now=None):
        with self.connection(True) as conn:
            return bool(conn.execute("""UPDATE cm_dispatch_events SET acknowledged_at=?
                WHERE id=? AND acknowledged_at IS NULL""", (stamp(now), event_id)).rowcount)

    def crawl_status(self, scheduled_sources=(), live_peers=()):
        names = {selector(name) for name in scheduled_sources}
        with self.connection() as conn:
            known = {}
            for name in names:
                row = conn.execute("""SELECT source_peer_id FROM cm_crawl_runs WHERE source_name=?
                    AND source_peer_id IS NOT NULL ORDER BY started_at DESC LIMIT 1""", (name,)).fetchone()
                if row:
                    known[name] = row[0]
            result = []
            for row in conn.execute("SELECT * FROM cm_rules ORDER BY id"):
                item = dict(row)
                enrolled = any(known.get(name) == row["source_peer_id"] or
                    (name not in known and name == selector(row["source_name"])) for name in names)
                status = row["state"]
                if status == "active":
                    status = "globally_stopped" if not self.enabled(conn) else "crawling" if row["source_peer_id"] in live_peers else (
                        "scheduled" if enrolled else "waiting_not_scheduled")
                item.update(crawl_status=status, scheduled=enrolled)
                result.append(item)
            return result
