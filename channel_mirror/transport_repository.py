"""Fenced publication checkpoints. A copied album is never copied twice."""

import json

from .models import SendOutcome, stamp


class TransportRepository:
    def claim_sendable(self, job):
        with self.connection() as conn:
            return bool(conn.execute("""SELECT 1 FROM cm_deliveries d JOIN cm_rules r ON r.id=d.rule_id
                WHERE d.id=? AND d.claim_token=? AND d.status='sending' AND d.lease_until>?
                AND r.state='active' AND r.destination_chat_id=d.destination_chat_id
                AND r.source_peer_id=?""", (job["id"], job["claim_token"], stamp(), job["source_peer_id"])).fetchone())

    def checkpoint_media(self, job, message_ids, *, now=None):
        ids = SendOutcome("sent", tuple(message_ids)).message_ids
        ts = stamp(now)
        stage = "caption_pending" if len(ids) == len(job["items"]) else "partial"
        with self.connection(True) as conn:
            changed = conn.execute("""UPDATE cm_deliveries SET destination_message_ids=?,publish_stage=?,
                media_sent_at=COALESCE(media_sent_at,?),updated_at=?
                WHERE id=? AND status='sending' AND claim_token=? AND lease_until>?""",
                (json.dumps(ids), stage, ts, ts, job["id"], job["claim_token"], ts)).rowcount
            conn.execute("""UPDATE cm_delivery_attempts SET destination_message_ids=?
                WHERE delivery_id=? AND claim_token=?""", (json.dumps(ids), job["id"], job["claim_token"]))
            return bool(changed)
