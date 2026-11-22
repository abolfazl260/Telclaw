"""Administration queries and a persistent global publication interlock."""
import json
import re
import config
from .models import stamp


def redact(value):
    text = str(value)
    token = config.TELEGRAM_BOT_TOKEN
    if token:
        text = text.replace(token, '[redacted]')
    return re.sub(r'\b\d{5,}:[A-Za-z0-9_-]{10,}\b', '[redacted]', text)


class AdminRepository:
    def enabled(self, conn=None):
        if conn is None:
            with self.connection() as conn:
                return self.enabled(conn)
        row = conn.execute("SELECT value FROM cm_settings WHERE key='enabled'").fetchone()
        return not row or row[0] == '1'

    def global_switch(self, enabled, actor):
        with self.connection(True) as conn:
            previous = self.enabled(conn)
            conn.execute("INSERT INTO cm_settings VALUES('enabled',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ('1' if enabled else '0',))
            if enabled and not previous:
                conn.execute("UPDATE cm_rules SET activation_watermark=MAX(COALESCE(activation_watermark,''),?),updated_at=?,updated_by=? WHERE state='active'", (stamp(), stamp(), str(actor)))
            self._audit(conn, actor, 'global_enable' if enabled else 'global_stop', 0, {}, stamp())

    def dashboard(self):
        with self.connection() as conn:
            counts = {}
            for row in conn.execute('SELECT rule_id,status,COUNT(*) AS n FROM cm_deliveries GROUP BY rule_id,status'):
                counts.setdefault(row['rule_id'], {})[row['status']] = row['n']
            rules = []
            for row in conn.execute('SELECT * FROM cm_rules ORDER BY id DESC'):
                item = dict(row)
                item['counts'] = counts.get(row['id'], {})
                last = conn.execute("SELECT finished_at FROM cm_crawl_runs WHERE source_peer_id=? AND state='completed' ORDER BY finished_at DESC LIMIT 1", (row['source_peer_id'],)).fetchone()
                item['last_crawl'] = last[0] if last else None
                check = conn.execute("SELECT created_at FROM cm_audit WHERE entity_id=? AND action='permission_verified' ORDER BY id DESC LIMIT 1", (row['id'],)).fetchone()
                item['permission_checked'] = check[0] if check else 'Not checked in this panel'
                error = conn.execute("SELECT error FROM cm_deliveries WHERE rule_id=? AND error<>'' ORDER BY updated_at DESC LIMIT 1", (row['id'],)).fetchone()
                item['last_error'] = redact(error[0]) if error else ''
                rules.append(item)
            return rules

    def history(self, rule_id, offset=0):
        with self.connection() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM cm_deliveries WHERE rule_id=? ORDER BY id DESC LIMIT 50 OFFSET ?', (rule_id, max(0, int(offset))))]
