#!/usr/bin/env python3
"""Shared helpers for the invoice agent. Data lives OUTSIDE the git repo (/home/ami/invoices)."""
import json, os, re, time
from pathlib import Path

BASE = Path(os.environ.get('INVOICE_BASE', '/home/ami/invoices'))
INBOX = BASE / 'inbox'
PENDING = BASE / 'pending'
FILED = BASE / 'filed'
REJECTED = BASE / 'rejected'
FAILED = BASE / 'failed'
STATE_DIR = BASE / 'state'
STATE = STATE_DIR / 'state.json'
HIST = STATE_DIR / 'history.json'
REPORT = STATE_DIR / 'last-report.json'

WAHA = os.environ.get('WAHA_URL', 'http://localhost:3000')
KEY = os.environ.get('WAHA_API_KEY', '')
SESSION = os.environ.get('WAHA_SESSION', 'default')

for d in (INBOX, PENDING, FILED, REJECTED, FAILED, STATE_DIR):
    d.mkdir(parents=True, exist_ok=True)


def load(p, default=None):
    try:
        return json.loads(Path(p).read_text('utf-8'))
    except Exception:
        return default


def save(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), 'utf-8')
    tmp.replace(p)


def default_state():
    return {
        'settings': {
            'fixed_tol_pct': 2,        # supplier with a fixed price: any change above this % needs review
            'variable_pct': 25,        # variable supplier (3+ invoices): % deviation that needs review
            'variable_min_abs': 50,    # ...and only if the absolute difference is also above this
            'few_history_pct': 50,     # supplier with 1-2 invoices on record
            'high_amount': 5000,       # any invoice above this always needs review
            'reminder_hours': 24,
        },
        'email_seen': [],
        'wa_watermarks': {},
        'wa_reply_watermark': 0,
        'pending': {},
        'notified': {},      # whatsapp message id (full and short) -> pending code
        'agent_sent_ids': [],
        'next_code': 1,
        'last_reminder': 0,
    }


def get_state():
    st = load(STATE, None)
    base = default_state()
    if not st:
        return base
    for k, v in base.items():
        st.setdefault(k, v)
    for k, v in base['settings'].items():
        st['settings'].setdefault(k, v)
    return st


def safe(s):
    s = re.sub(r'[\\/:*?"<>|\s]+', '_', str(s or '')).strip('_')
    return s[:60] or 'unknown'


def wa(method, path, **kw):
    import requests
    timeout = kw.pop('timeout', 90)
    r = requests.request(method, WAHA + path, headers={'X-Api-Key': KEY}, timeout=timeout, **kw)
    r.raise_for_status()
    return r


def self_chat_id():
    me = wa('GET', f'/api/sessions/{SESSION}').json().get('me') or {}
    return me.get('lid') or me.get('id')


def short_id(mid):
    """WAHA ids look like true_<chat>_<MSGID>_out (self-chat) or bare <MSGID> in replyTo."""
    s = str(mid or '')
    if s.endswith('_out'):
        s = s[:-4]
    return s.split('_')[-1]
