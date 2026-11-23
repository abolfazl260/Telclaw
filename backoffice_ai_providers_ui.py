"""Read-only HTML presentation for Back Office AI provider cooldown diagnostics.

Dynamic values are escaped on initial HTML render and set via textContent
when polling. Authentication and CSP are supplied by the existing Back Office.
"""
from html import escape


STYLE = """
.ai-status-head{display:flex;align-items:center;justify-content:space-between;gap:.8rem;flex-wrap:wrap}
.ai-status-head h2{margin:0}.ai-status-head button{width:auto}
.ai-provider-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:.8rem;margin:.8rem 0}
.ai-status-card{border:1px solid #dce4ec;border-radius:12px;padding:1rem;min-width:0;background:#fafcff}
.ai-status-card h3{margin:0}.ai-status-card p{margin:.55rem 0;overflow-wrap:anywhere}
.ai-card-head{display:flex;justify-content:space-between;gap:.5rem;align-items:center}
.ai-status-card .muted{font-size:.84rem}
"""

SCRIPT = """
(function () {
  const root = document.getElementById('ai-provider-status');
  const note = document.getElementById('ai-provider-refresh-note');
  const refreshButton = document.getElementById('ai-provider-refresh');
  const field = (card, name, value) => {
    const el = card.querySelector('[data-field="' + name + '"]');
    if (el) el.textContent = value;
  };
  const lastCooldown = (event) => {
    if (!event) return 'Not recorded';
    const when = event.at ? ' · ' + new Date(event.at).toLocaleString() : '';
    return event.label + when + (event.active ? ' (active)' : ' (past)');
  };
  const accountLabel = (a) => a.label + ': ' + (a.model || 'model unknown') +
    (a.cooldown_seconds > 0 ? ' (cooldown ' + a.cooldown_seconds.toFixed(1) + 's)' : '') +
    (a.last_cooldown ? ' — ' + lastCooldown(a.last_cooldown) : '');
  const tokenLabel = (p) => {
    if (p.name !== 'groq') return 'Unknown — no token balance is exposed for Workers AI';
    const t = p.tokens;
    const local = t.available_estimate == null
      ? 'Unknown local usage · ' + t.safe_tpm.toLocaleString() + ' configured safe TPM'
      : t.available_estimate.toLocaleString() + ' estimated in local budget / ' +
        t.safe_tpm.toLocaleString() + ' safe TPM';
    if (!t.usable_now) {
      const restriction = p.state === 'Cooldown' ? 'On cooldown' :
        p.state === 'Disabled' ? 'Disabled' :
        p.state === 'Missing credentials' ? 'Missing credentials' : 'Not verified';
      return restriction + ' — not currently usable. ' + local;
    }
    return local;
  };
  async function refresh() {
    try {
      const res = await fetch('/settings/ai-providers/status', {
        credentials: 'same-origin', cache: 'no-store'
      });
      if (!res.ok) throw new Error('status unavailable');
      const data = await res.json();
      for (const p of data.providers) {
        const card = root.querySelector('[data-provider="' + p.name + '"]');
        if (!card) continue;
        field(card, 'priority', p.priority == null ? 'Disabled' : 'Priority ' + p.priority);
        field(card, 'state', p.state);
        field(card, 'selected', p.selection_label);
        field(card, 'count', String(p.credential_count));
        field(card, 'cooldown', p.cooldown_seconds > 0 ? p.cooldown_seconds.toFixed(1) + 's' : 'None');
        field(card, 'reason', lastCooldown(p.last_cooldown));
        field(card, 'accounts', p.accounts.map(accountLabel).join('; ') || 'No credentials configured');
        field(card, 'tokens', tokenLabel(p));
      }
      note.textContent = 'Updated ' + new Date().toLocaleTimeString() +
        (data.requires_restart ? ' · Provider priority changed; restart required.' : '');
    } catch (_) {
      note.textContent = 'Status refresh failed. Reload this page to retry.';
    }
  }
  refreshButton.addEventListener('click', refresh);
  setInterval(function () { if (!document.hidden) refresh(); }, 15000);
})();
"""


def _last_cooldown(event):
    if not event:
        return "Not recorded"
    time = event["at"] or ""
    when = " · " + time.replace("T", " ")[:19] + " UTC" if time else ""
    status = "active" if event["active"] else "past"
    return f'{event["label"]}{when} ({status})'


def _account_label(account):
    wait = account["cooldown_seconds"]
    return (
        f'{account["label"]}: {account["model"] or "model unknown"}'
        + (f' (cooldown {wait:.1f}s)' if wait > 0 else "")
        + (f' — {_last_cooldown(account["last_cooldown"])}'
           if account["last_cooldown"] else "")
    )


def _tokens_label(provider):
    if provider["name"] != "groq":
        return "Unknown — no token balance is exposed for Workers AI"
    tokens = provider["tokens"]
    if tokens["available_estimate"] is None:
        local = f'Unknown local usage · {tokens["safe_tpm"]:,} configured safe TPM'
    else:
        local = (f'{tokens["available_estimate"]:,} estimated in local budget / '
                 f'{tokens["safe_tpm"]:,} safe TPM')
    if not tokens["usable_now"]:
        restriction = {
            "Cooldown": "On cooldown",
            "Disabled": "Disabled",
            "Missing credentials": "Missing credentials",
        }.get(provider["state"], "Not verified")
        return f"{restriction} — not currently usable. {local}"
    return local


def _card(provider):
    name = provider["name"]
    priority = f'Priority {provider["priority"]}' if provider["priority"] else "Disabled"
    selected = provider["selection_label"]
    accounts = "; ".join(map(_account_label, provider["accounts"])) or "No credentials configured"
    cooldown = (f'{provider["cooldown_seconds"]:.1f}s'
                if provider["cooldown_seconds"] > 0 else "None")
    note = ("One local TPM budget shared across Groq keys and AI stages; "
            "estimated tokens are not a live Groq account balance." if name == "groq" else
            "Cloudflare's remaining token quota is not exposed by this integration.")
    return f"""<article class="ai-status-card" data-provider="{escape(name, quote=True)}">
      <div class="ai-card-head"><h3>{escape(name.title())}</h3>
      <span class="source default" data-field="priority">{escape(priority)}</span></div>
      <p><strong>Routing status:</strong> <span data-field="state">{escape(provider["state"])}</span></p>
      <p><strong>Selected for:</strong> <span data-field="selected">{escape(selected)}</span></p>
      <p><strong>Configured credentials:</strong> <span data-field="count">{provider["credential_count"]}</span></p>
      <p><strong>Cooldown:</strong> <span data-field="cooldown">{escape(cooldown)}</span></p>
      <p><strong>Last cooldown cause:</strong> <span data-field="reason">{escape(_last_cooldown(provider["last_cooldown"]))}</span></p>
      <p><strong>Models / accounts:</strong> <span data-field="accounts">{escape(accounts)}</span></p>
      <p><strong>Tokens available:</strong> <span data-field="tokens">{escape(_tokens_label(provider))}</span></p>
      <p class="muted">{escape(note)}</p>
    </article>"""


def render(snapshot, nonce):
    """Render a passive initial snapshot and authenticated 15s refresh."""
    cards = "".join(_card(item) for item in snapshot["providers"])
    restart_note = (
        '<p class="restart">Provider priority changed after client initialization; restart required.</p>'
        if snapshot["requires_restart"] else ""
    )
    return f"""<style>{STYLE}</style>
    <section id="ai-provider-status" aria-label="AI Provider Status">
      <div class="ai-status-head"><h2>AI Provider Status</h2>
      <button type="button" class="secondary" id="ai-provider-refresh">Refresh status</button></div>
      <p class="muted">Process-local status only. No external calls are made while viewing this panel.
      Eligible means not on cooldown; it does not confirm a working API key or model access.
      Cooldown reasons are recorded only for events after this version starts; longer active
      cooldown deadlines retain their original cause. Other processes and hosts are not included.</p>
      <div class="ai-provider-grid">{cards}</div>
      {restart_note}
      <p class="muted" id="ai-provider-refresh-note" aria-live="polite">
      Snapshot on page load · refreshes automatically every 15 seconds.</p>
    </section>
    <script nonce="{escape(nonce, quote=True)}">{SCRIPT}</script>"""
