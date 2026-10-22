#!/usr/bin/env python3
"""Fetch invoice-looking emails over IMAP (Gmail app password) into the inbox queue.
Credentials: /home/ami/.invoice-imap  (line 1 = gmail address, line 2 = app password), chmod 600.
Usage: invoice_fetch_email.py [DAYS=3]
"""
import imaplib, email, email.utils, sys, json, hashlib, re
from email.header import decode_header, make_header
from invoice_common import *

CRED = Path('/home/ami/.invoice-imap')
CARD_DOMAINS = [d for s_ in ((load(STATE_DIR / 'supplier_map.json', {}) or {}).get('card_notices') or {}).get('sources', []) for d in s_['domains']]
OKEXT = {'.pdf', '.jpg', '.jpeg', '.png', '.heic', '.webp'}
MAX_ITEMS = 40


def dh(v):
    try:
        return str(make_header(decode_header(v or '')))
    except Exception:
        return v or ''


def body_text(msg):
    parts = []
    for p in msg.walk():
        if p.get_content_type() == 'text/plain' and not p.get_filename():
            try:
                parts.append(p.get_payload(decode=True).decode(p.get_content_charset() or 'utf-8', 'replace'))
            except Exception:
                pass
    if not parts:
        for p in msg.walk():
            if p.get_content_type() == 'text/html' and not p.get_filename():
                try:
                    h = p.get_payload(decode=True).decode(p.get_content_charset() or 'utf-8', 'replace')
                    parts.append(re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', h)))
                except Exception:
                    pass
    return '\n'.join(parts)[:20000]


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    if not CRED.exists():
        print(json.dumps({'ok': False, 'error': 'missing-credentials-file'}))
        return 2
    lines = CRED.read_text().split('\n')
    user, pw = lines[0].strip(), lines[1].strip().replace(' ', '')
    st = get_state()
    seen = set(st['email_seen'])
    M = imaplib.IMAP4_SSL('imap.gmail.com')
    M.login(user, pw)
    M.select('"[Gmail]/All Mail"', readonly=True)
    q = f'(חשבונית OR קבלה OR invoice OR receipt OR "חשבון חוזה") newer_than:{days}d -in:sent -in:drafts -in:spam -in:trash'
    M.literal = q.encode('utf-8')
    typ, data = M.uid('SEARCH', 'CHARSET', 'UTF-8', 'X-GM-RAW')
    uids = data[0].split() if typ == 'OK' and data and data[0] else []
    added = skipped = 0
    for uid in uids[-200:]:
        if added >= MAX_ITEMS:
            break
        typ, md = M.uid('FETCH', uid, '(RFC822)')
        if typ != 'OK' or not md or not md[0]:
            continue
        msg = email.message_from_bytes(md[0][1])
        mid = (msg.get('Message-ID') or '').strip()
        if any(dm in (msg.get('From') or '').lower() for dm in CARD_DOMAINS) and re.search(r'חיוב|פירוט', dh(msg.get('Subject'))):
            continue
        if not mid or mid in seen:
            skipped += 1
            continue
        item_id = 'email-' + hashlib.sha1(mid.encode()).hexdigest()[:10]
        d = INBOX / item_id
        d.mkdir(parents=True, exist_ok=True)
        files = []
        for p in msg.walk():
            fn = dh(p.get_filename())
            if not fn:
                continue
            ext = Path(fn).suffix.lower()
            payload = p.get_payload(decode=True)
            if ext not in OKEXT or not payload or len(payload) < 4000:
                continue
            name = safe(Path(fn).stem) + ext
            (d / name).write_bytes(payload)
            files.append(name)
        (d / 'body.txt').write_text(body_text(msg), 'utf-8')
        save(d / 'meta.json', {
            'item_id': item_id, 'source': 'email', 'from': dh(msg.get('From')), 'subject': dh(msg.get('Subject')),
            'date': (email.utils.parsedate_to_datetime(msg['Date']).date().isoformat() if msg.get('Date') else None),
            'files': files, 'message_id': mid,
            'link': 'https://mail.google.com/mail/u/0/#search/rfc822msgid%3A' + mid.strip('<>')})
        seen.add(mid)
        added += 1
    st['email_seen'] = list(seen)[-3000:]
    save(STATE, st)
    M.logout()
    print(json.dumps({'ok': True, 'matched': len(uids), 'added': added, 'already_seen': skipped}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
