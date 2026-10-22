#!/usr/bin/env python3
"""Apply the user's EXISTING Gmail invoice labels (never creates labels) to filed invoice emails.
Usage: invoice_label.py [--backfill-ids] [--include-old] [--only-supplier NAME]
 default: label filed email records that have a message_id and no 'labeled' mark (new invoices only).
 --backfill-ids: fill message_id on older records (mailbox untouched); they are marked old and NOT labeled unless --include-old.
"""
import imaplib, re, sys, json, base64, hashlib
from invoice_common import *
import invoice_decide as D


def menc(s):
    out, buf = [], ''
    def flush():
        nonlocal buf
        if buf:
            out.append('&' + base64.b64encode(buf.encode('utf-16-be')).decode().rstrip('=').replace('/', ',') + '-')
            buf = ''
    for ch in s:
        if 0x20 <= ord(ch) <= 0x7e:
            flush(); out.append('&-' if ch == '&' else ch)
        else:
            buf += ch
    flush()
    return ''.join(out)


def mdec(x):
    def f(m):
        t = m.group(1)
        if t == '':
            return '&'
        t = t.replace(',', '/'); t += '=' * (-len(t) % 4)
        return base64.b64decode(t).decode('utf-16-be')
    return re.sub(r'&([^-]*)-', f, x)


def connect(readonly):
    l = open('/home/ami/.invoice-imap').read().split('\n')
    M = imaplib.IMAP4_SSL('imap.gmail.com')
    M.login(l[0].strip(), l[1].strip().replace(' ', ''))
    M.select('"[Gmail]/All Mail"', readonly=readonly)
    return M


def existing_labels(M):
    names = set()
    t, d = M.list()
    for line in d or []:
        s = line.decode('utf-8', 'replace') if isinstance(line, bytes) else str(line)
        m = re.search(r'"/" (?:"([^"]*)"|(\S+))\s*$', s)
        if m:
            names.add(mdec(m.group(1) or m.group(2)))
    return names


def main():
    args = sys.argv[1:]
    only = args[args.index('--only-supplier') + 1] if '--only-supplier' in args else None
    hist = load(HIST, [])
    mp = D.supplier_map()
    res = {'backfilled': 0, 'labeled': 0, 'skipped': []}
    if '--backfill-ids' in args:
        M = connect(True)
        need = {h['item_id'] for h in hist if h['source'] == 'email' and not h.get('message_id')}
        M.literal = 'newer_than:120d'.encode()
        t, d = M.uid('SEARCH', 'CHARSET', 'UTF-8', 'X-GM-RAW')
        for u in d[0].split():
            t, md = M.uid('FETCH', u, '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
            m = re.search(r'Message-ID:\s*(\S+)', md[0][1].decode('utf-8', 'replace'), re.I) if md and md[0] else None
            if not m:
                continue
            iid = 'email-' + hashlib.sha1(m.group(1).strip().encode()).hexdigest()[:10]
            if iid in need:
                for h in hist:
                    if h['item_id'] == iid:
                        h['message_id'] = m.group(1).strip()
                        h.setdefault('labeled', 'old')
                        res['backfilled'] += 1
        M.logout()
        save(HIST, hist)
    archive = mp.get('archive_filed_from_inbox', True)
    include_old = '--include-old' in args
    todo = [h for h in hist if h['status'] == 'filed' and h['source'] == 'email' and h.get('message_id')
            and (h.get('labeled') != 'old' or include_old)
            and (not h.get('labeled') or h.get('labeled') == 'old' or (archive and not h.get('archived')))
            and (not only or h['supplier'] == only)]
    if todo:
        M = connect(False)
        have = existing_labels(M)
        for h in todo:
            label = mp.get('labels', {}).get(h['supplier']) or mp.get('default_label', 'חשבוניות')
            need_label = (not h.get('labeled')) or h.get('labeled') == 'old'
            if need_label and label not in have:
                res['skipped'].append(f"{h['supplier']}: התווית '{label}' לא קיימת")
                continue
            t, d = M.uid('SEARCH', 'HEADER', 'Message-ID', '"' + h['message_id'].strip('<>') + '"')
            uids = d[0].split() if t == 'OK' and d and d[0] else []
            if not uids:
                res['skipped'].append(f"{h['supplier']}: המייל לא נמצא")
                continue
            if need_label:
                t, r = M.uid('STORE', uids[0], '+X-GM-LABELS', '("' + menc(label) + '")')
                if t == 'OK':
                    h['labeled'] = label
                    res['labeled'] += 1
                else:
                    continue
            if archive and h.get('labeled') and h.get('labeled') != 'old' and not h.get('archived'):
                t, r = M.uid('STORE', uids[0], '-X-GM-LABELS', '(\\Inbox)')
                if t == 'OK':
                    h['archived'] = True
                    res['archived'] = res.get('archived', 0) + 1
        M.logout()
        save(HIST, hist)
    print(json.dumps(res, ensure_ascii=False))


if __name__ == '__main__':
    main()
