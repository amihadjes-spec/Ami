#!/usr/bin/env python3
"""WhatsApp side of the invoice agent (self-chat only).
Usage: invoice_wa.py notify | process-replies | send-text "TEXT"
"""
import sys, json, base64, time, re, mimetypes
from invoice_common import *
import invoice_decide as D

YES = {'כן', 'אשר', 'מאשר', 'אישור', 'אשרתי', 'yes', 'y', 'ok', 'אוקיי', 'בסדר', 'יאללה', '👍'}
NO = {'לא', 'דחה', 'דחייה', 'דחיה', 'לדחות', 'no', 'n', 'בטל', '👎'}


def mid_of(j):
    i = j.get('id')
    if isinstance(i, dict):
        i = i.get('_serialized') or i.get('id')
    return i


def remember(st, mid, code=None):
    if not mid:
        return
    st['agent_sent_ids'] = (st.get('agent_sent_ids', []) + [mid, short_id(mid)])[-400:]
    if code is not None:
        st['notified'][mid] = str(code)
        st['notified'][short_id(mid)] = str(code)


def send_text(st, text, code=None):
    j = wa('POST', '/api/sendText', json={'session': SESSION, 'chatId': self_chat_id(), 'text': text}).json()
    remember(st, mid_of(j), code)
    return mid_of(j)


def send_file(st, path, caption, code=None):
    p = Path(path)
    mt = mimetypes.guess_type(p.name)[0] or 'application/octet-stream'
    if not (mt == 'application/pdf' or mt.startswith('image/')):
        return send_text(st, caption, code)
    body = {'session': SESSION, 'chatId': self_chat_id(), 'caption': caption,
            'file': {'mimetype': mt, 'filename': p.name, 'data': base64.b64encode(p.read_bytes()).decode()}}
    try:
        j = wa('POST', '/api/sendFile', json=body, timeout=180).json()
    except Exception:
        return send_text(st, caption, code)
    remember(st, mid_of(j), code)
    return mid_of(j)


def money(a, cur):
    return D.fmt(a) + (' ש"ח' if cur == 'ILS' else ' ' + str(cur))


def notify():
    rep = load(REPORT, {}) or {}
    st = get_state()
    if os.environ.get('INVOICE_QUIET') == '1':
        return
    if st.get('outbox'):
        send_text(st, 'סוכן החשבוניות\n' + '\n'.join(st['outbox']))
        st['outbox'] = []
        save(STATE, st)
    if rep.get('notified'):
        return
    if rep.get('baseline'):
        send_text(st, f"סוכן החשבוניות: סריקת בסיס הסתיימה. {len(rep.get('filed', []))} חשבוניות נקלטו להיסטוריה ותויקו, בלי התראות. מכאן והלאה כל חשבונית חדשה תיבדק מול ההיסטוריה.")
        rep['notified'] = True
        save(REPORT, rep); save(STATE, st)
        return
    lines = []
    if rep.get('filed'):
        lines.append(f"חשבוניות שתויקו ({len(rep['filed'])}), ללא שינוי מהותי:")
        for f in rep['filed']:
            lines.append(f"- {f['supplier']} | {f['doc_date']} | {money(f['amount'], f['currency'])}" + (f" | מס' {f['doc_no']}" if f.get('doc_no') else ''))
    dups = [x for x in rep.get('duplicate', [])]
    if dups:
        lines.append(f"כפילויות שדולגו ({len(dups)}):")
        for x in dups:
            lines.append(f"- {x['supplier']} | {x.get('doc_no') or x['doc_date']}" + (' (נדחתה בעבר)' if x.get('rejected') else ''))
    if rep.get('errors'):
        lines.append(f"שגיאות ({len(rep['errors'])}):")
        for e in rep['errors']:
            lines.append(f"- {e.get('subject') or e['item']}: {e['error']}")
    if lines:
        send_text(st, 'סוכן החשבוניות\n' + '\n'.join(lines))
    for r in rep.get('review', []):
        ref = r.get('ref') or {}
        cap = [f"חשבונית לבדיקה #{r['code']}", f"ספק: {r['supplier']}" + (f" ({r['account_ref']})" if r.get('account_ref') else ''),
               f"תאריך: {r['doc_date']}" + (f" | מס' {r['doc_no']}" if r.get('doc_no') else ''), f"סכום: {money(r['amount'], r['currency'])}"]
        if ref.get('amount') is not None:
            cap.append(f"להשוואה: {money(ref['amount'], r['currency'])} ({ref.get('label', 'קודמת')})")
        cap.append('סיבה: ' + '; '.join(r['reasons']))
        cap.append(f"מקור: {'וואטסאפ' if r.get('source') == 'whatsapp' else 'מייל'}" + (f" - {r['subject']}" if r.get('subject') else ''))
        cap.append('השב (Reply) על ההודעה: כן לאישור ותיוק, לא לדחייה. או כתוב: אשר %s / דחה %s' % (r['code'], r['code']))
        send_file(st, (r.get('files') or [''])[0], '\n'.join(cap), r['code']) if r.get('files') else send_text(st, '\n'.join(cap), r['code'])
    # reminder for old pending items
    pend = st['pending']
    if pend and not rep.get('review') and time.time() - st.get('last_reminder', 0) > st['settings']['reminder_hours'] * 3600:
        old = [c for c, p in pend.items() if time.time() - p['created'] > 20 * 3600]
        if old:
            send_text(st, 'תזכורת: חשבוניות שממתינות לאישור: ' + ', '.join('#' + c for c in old) + '. השב כן או לא על ההודעה המקורית, או כתוב: אשר N / דחה N')
            st['last_reminder'] = time.time()
    rep['notified'] = True
    save(REPORT, rep)
    save(STATE, st)


def classify(text):
    t = re.sub(r'[^\w\s👍👎]', ' ', str(text or '')).strip().lower()
    first = t.split()[0] if t.split() else ''
    if t in YES or first in YES and len(t.split()) <= 2:
        return 'approve'
    if t in NO or first in NO and len(t.split()) <= 2:
        return 'reject'
    return None


def process_replies():
    st = get_state()
    chat = self_chat_id()
    wm = st.get('wa_reply_watermark') or int(time.time()) - 3 * 86400
    msgs = wa('GET', f'/api/{SESSION}/chats/{chat}/messages', params={'limit': 100, 'downloadMedia': 'false', 'filter.timestamp.gte': wm + 1}, timeout=120).json() or []
    msgs.sort(key=lambda m: m.get('timestamp') or 0)
    top = wm
    decisions, unknown = {}, []
    for m in msgs:
        ts = m.get('timestamp') or 0
        top = max(top, ts)
        mid = m.get('id') if isinstance(m.get('id'), str) else (m.get('id') or {}).get('_serialized', '')
        if mid in st['agent_sent_ids'] or short_id(mid) in st['agent_sent_ids']:
            continue
        body = m.get('body') or ''
        rid = (m.get('replyTo') or {}).get('id') if isinstance(m.get('replyTo'), dict) else None
        code = None
        if rid:
            code = st['notified'].get(rid) or st['notified'].get(short_id(rid))
        mm = re.match(r'^\s*(אשר|כן|מאשר|דחה|לא)\s*#?(\d+)\s*$', body)
        action = classify(body)
        if mm:
            code = mm.group(2)
            action = 'approve' if mm.group(1) in ('אשר', 'כן', 'מאשר') else 'reject'
        if not code:
            continue
        if action:
            decisions[code] = action   # sorted by time: last reply wins
        else:
            unknown.append(code)
    out = []
    for code, action in decisions.items():
        if code in st['pending']:
            out.append(D.approve(code) if action == 'approve' else D.reject(code))
    st = get_state()  # decide.* rewrote state
    for code in set(unknown) - set(decisions):
        if code in st['pending']:
            out.append(f'לא הבנתי את התשובה על #{code}. השב כן או לא.')
    st['wa_reply_watermark'] = top
    if out:
        if os.environ.get('INVOICE_QUIET') == '1':
            st['outbox'] = st.get('outbox', []) + out
        else:
            send_text(st, 'סוכן החשבוניות\n' + '\n'.join(out))
    save(STATE, st)
    print(json.dumps({'processed': len(decisions), 'unknown': len(unknown)}, ensure_ascii=False))


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd == 'notify':
        notify()
    elif cmd == 'process-replies':
        process_replies()
    elif cmd == 'send-text':
        st = get_state(); send_text(st, sys.argv[2]); save(STATE, st)
    else:
        print(__doc__)
