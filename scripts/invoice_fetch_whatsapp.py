#!/usr/bin/env python3
"""Fetch PDF/image invoice candidates from WhatsApp (WAHA) into the inbox queue.
Usage: invoice_fetch_whatsapp.py [BACKFILL_DAYS=3]   (used only for chats with no watermark yet)
"""
import sys, json, hashlib, sqlite3, os, re, time
from invoice_common import *

STORE = os.environ.get('WAHA_NOWEB_STORE_PATH', '/home/ami/waha-sessions/noweb/default/store.sqlite3')
KW = re.compile(r'חשבונית|קבלה|invoice|receipt|חיוב|לתשלום', re.I)
WA_STATE = Path('/home/ami/Ami/state/whatsapp-event-agent-state.json')


def chat_ids():
    ids = []
    try:
        db = sqlite3.connect(f'file:{STORE}?mode=ro', uri=True)
        ids = [r[0] for r in db.execute('SELECT id FROM chats') if r[0]]
        db.close()
    except Exception:
        pass
    if not ids:
        try:
            ids = [c['id'] if isinstance(c['id'], str) else c['id'].get('_serialized') for c in wa('GET', f'/api/{SESSION}/chats/overview').json()]
        except Exception:
            ids = []
    return ids


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    st = get_state()
    me = self_chat_id()
    muted = set((load(WA_STATE, {}) or {}).get('muted_chat_ids', []))
    sent_ids = set(st.get('agent_sent_ids', [])) | set(st.get('notified', {}).keys())
    ids = chat_ids()
    if me and me not in ids:
        ids.append(me)
    now = int(time.time())
    added = errors = scanned = 0
    for cid in ids:
        if cid in muted or cid.endswith('@newsletter') or cid == 'status@broadcast':
            continue
        wm = st['wa_watermarks'].get(cid, now - days * 86400)
        try:
            msgs = wa('GET', f'/api/{SESSION}/chats/{cid}/messages', params={'limit': 100, 'downloadMedia': 'true', 'filter.timestamp.gte': wm + 1}, timeout=180).json()
        except Exception:
            errors += 1
            continue
        scanned += 1
        top = wm
        for m in msgs or []:
            ts = m.get('timestamp') or 0
            top = max(top, ts)
            mid = m.get('id') if isinstance(m.get('id'), str) else (m.get('id') or {}).get('_serialized', '')
            if not m.get('hasMedia'):
                continue
            if m.get('fromMe') and cid != me:
                continue
            if mid in sent_ids or short_id(mid) in sent_ids or str(m.get('body') or '').startswith('חשבונית לבדיקה'):
                continue
            media = m.get('media') or {}
            mt = (media.get('mimetype') or '').lower()
            fname = media.get('filename') or ''
            caption = m.get('body') or ''
            is_pdf = 'pdf' in mt or fname.lower().endswith('.pdf')
            is_img = mt.startswith('image/') and 'webp' not in mt
            if not (is_pdf or (is_img and (cid == me or KW.search(caption + fname)))):
                continue
            url = media.get('url')
            if not url:
                continue
            url = re.sub(r'^https?://[^/]+', WAHA, url)
            try:
                data = wa('GET', url[len(WAHA):], timeout=120).content
            except Exception:
                errors += 1
                continue
            if len(data) < 4000:
                continue
            item_id = 'wa-' + hashlib.sha1(mid.encode()).hexdigest()[:10]
            d = INBOX / item_id
            d.mkdir(parents=True, exist_ok=True)
            ext = '.pdf' if is_pdf else ('.png' if 'png' in mt else '.jpg')
            name = safe(Path(fname).stem or item_id) + ext
            (d / name).write_bytes(data)
            (d / 'body.txt').write_text(caption, 'utf-8')
            save(d / 'meta.json', {'item_id': item_id, 'source': 'whatsapp', 'chat': cid, 'from': m.get('from'),
                                   'caption': caption, 'date': time.strftime('%Y-%m-%d', time.localtime(ts)) if ts else None,
                                   'files': [name], 'message_id': mid})
            added += 1
        st['wa_watermarks'][cid] = top
    save(STATE, st)
    print(json.dumps({'ok': True, 'chats_scanned': scanned, 'added': added, 'errors': errors}))


if __name__ == '__main__':
    main()
