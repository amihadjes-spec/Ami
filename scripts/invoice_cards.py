#!/usr/bin/env python3
"""Credit-card charge notices (Isracard, Cal) by email.
1. Forward the email to Nirit (skipped when the thread already contains a forward from Ami to her).
2. Ask Ami for approval in the WhatsApp self-chat (same code/reply mechanism as invoices).
3. After approval (handled by invoice_decide.approve + invoice_label.py): label the thread with the
   matching Gmail label and remove it from the Inbox.
Config: state/supplier_map.json -> card_notices {forward_to, days, sources:[{name, domains, label}]}
Does nothing in quiet hours (INVOICE_QUIET=1): no email, no WhatsApp.
"""
import imaplib, email, email.utils, re, os, sys, json, smtplib, time, datetime
from email.message import EmailMessage
from email.header import decode_header, make_header
from invoice_common import *
import invoice_wa as W
from invoice_label import mdec

CRED = Path('/home/ami/.invoice-imap')
SUBJ = re.compile(r'חיוב|פירוט')


def dh(v):
    try:
        return str(make_header(decode_header(v or '')))
    except Exception:
        return v or ''


def creds():
    l = CRED.read_text().split('\n')
    return l[0].strip(), l[1].strip().replace(' ', '')


def parse_labels(raw):
    m = re.search(r'X-GM-LABELS \((.*?)\)', raw)
    return [mdec(a or b) for a, b in re.findall(r'"([^"]+)"|(\S+)', m.group(1))] if m else []


def build_forward(orig, user, to):
    subj = dh(orig.get('Subject'))
    m = EmailMessage()
    m['From'] = user
    m['To'] = to
    m['Subject'] = 'Fwd: ' + subj
    m['Date'] = email.utils.formatdate(localtime=True)
    m.set_content('הודעת חיוב כרטיס אשראי מועברת אליך (נשלח אוטומטית מסוכן החשבוניות). המייל המקורי מצורף.\n\nנושא: ' + subj)
    m.add_attachment(orig)
    return m


def send(msg, user, pw):
    with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=60) as s:
        s.login(user, pw)
        s.send_message(msg)


def wa_text(code, p):
    fw = 'הועברה כבר לנירית (על ידך)' if p['forwarded'] == 'manual' else 'הועברה לנירית במייל'
    return '\n'.join([
        f'הודעת חיוב כרטיס אשראי #{code}',
        f"כרטיס: {p['name']}",
        f"נושא: {p['subject']}",
        f"תאריך: {p['date']}",
        fw,
        f"השב (Reply) על ההודעה: כן לתיוק בתיקיית {p['label']} והסרה מהדואר הנכנס, או לא לדחייה. או כתוב: אשר {code} / דחה {code}"])


def main():
    if os.environ.get('INVOICE_QUIET') == '1':
        print(json.dumps({'skipped': 'quiet-hours'}))
        return 0
    cfg = (load(STATE_DIR / 'supplier_map.json', {}) or {}).get('card_notices') or {}
    sources = cfg.get('sources', [])
    to = cfg.get('forward_to', 'nirithadjes@gmail.com')
    days = int(cfg.get('days', 5))
    if not sources:
        print(json.dumps({'skipped': 'no-sources'}))
        return 0
    user, pw = creds()
    st = get_state()
    seen = set(st.get('card_seen', []))
    res = {'found': 0, 'forwarded': 0, 'already_forwarded': 0, 'notified': 0, 'errors': []}
    M = imaplib.IMAP4_SSL('imap.gmail.com')
    M.login(user, pw)
    M.select('"[Gmail]/All Mail"', readonly=True)
    doms = ' OR '.join(d for s in sources for d in s['domains'])
    M.literal = f'from:({doms}) newer_than:{days}d'.encode()
    t, d = M.uid('SEARCH', 'CHARSET', 'UTF-8', 'X-GM-RAW')
    uids = d[0].split() if t == 'OK' and d and d[0] else []
    for u in uids:
        t, md = M.uid('FETCH', u, '(X-GM-THRID X-GM-LABELS RFC822)')
        if t != 'OK' or not md or not md[0]:
            continue
        head = md[0][0].decode('utf-8', 'replace')
        orig = email.message_from_bytes(md[0][1])
        mid = (orig.get('Message-ID') or '').strip()
        subj = dh(orig.get('Subject'))
        sender = (orig.get('From') or '').lower()
        src = next((s for s in sources if any(dm in sender for dm in s['domains'])), None)
        labs = parse_labels(head)
        if not src or not mid or mid in seen or not SUBJ.search(subj) or 'Sent' in ' '.join(labs):
            continue
        if src['label'] in labs:      # already filed
            seen.add(mid)
            continue
        m = re.search(r'X-GM-THRID (\d+)', head)
        thrid = m.group(1) if m else None
        res['found'] += 1
        # already forwarded to Nirit by Ami?
        forwarded = False
        if thrid:
            t2, d2 = M.uid('SEARCH', 'X-GM-THRID', thrid)
            for u2 in (d2[0].split() if t2 == 'OK' and d2 and d2[0] else []):
                t3, md3 = M.uid('FETCH', u2, '(X-GM-LABELS BODY.PEEK[HEADER.FIELDS (TO)])')
                if t3 == 'OK' and md3 and md3[0]:
                    raw3 = md3[0][0].decode('utf-8', 'replace') + md3[0][1].decode('utf-8', 'replace')
                    if 'Sent' in raw3 and to.lower() in raw3.lower():
                        forwarded = True
        how = 'manual'
        if not forwarded:
            try:
                send(build_forward(orig, user, to), user, pw)
                how = 'auto'
                res['forwarded'] += 1
            except Exception as e:
                res['errors'].append(f'forward failed: {e}')
                continue
        else:
            res['already_forwarded'] += 1
        code = st['next_code']
        st['next_code'] += 1
        dt = email.utils.parsedate_to_datetime(orig['Date']).date().isoformat() if orig.get('Date') else ''
        st['pending'][str(code)] = {'kind': 'card', 'name': src['name'], 'label': src['label'], 'message_id': mid,
                                    'thrid': thrid, 'subject': subj, 'date': dt, 'forwarded': how,
                                    'created': time.time(), 'wa_sent': False}
        seen.add(mid)
        st['card_seen'] = list(seen)[-500:]
        save(STATE, st)
    M.logout()
    # send WhatsApp requests (also retries ones that failed to send earlier)
    for code, p in list(st['pending'].items()):
        if p.get('kind') == 'card' and not p.get('wa_sent'):
            try:
                W.send_text(st, wa_text(code, p), code)
                p['wa_sent'] = True
                res['notified'] += 1
            except Exception as e:
                res['errors'].append(f'whatsapp failed for #{code}: {e}')
            save(STATE, st)
    st['card_seen'] = list(seen)[-500:]
    save(STATE, st)
    print(json.dumps(res, ensure_ascii=False))
    return 1 if res['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
