#!/usr/bin/env python3
"""Deterministic decision layer: compares extracted invoices to history, files or holds for review.
Usage: invoice_decide.py run [--baseline] | approve CODE | reject CODE | status | suppliers
"""
import sys, json, shutil, statistics, time, datetime, hashlib
from pathlib import Path
from invoice_common import *

CUR = {'₪': 'ILS', 'ש"ח': 'ILS', 'שח': 'ILS', 'NIS': 'ILS', 'ILS': 'ILS', '$': 'USD', 'USD': 'USD', '€': 'EUR', 'EUR': 'EUR'}


def pdate(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def supplier_map():
    return load(STATE_DIR / 'supplier_map.json', {'aliases': {}, 'labels': {}})


def canon(name):
    return supplier_map().get('aliases', {}).get((name or '').strip(), (name or '').strip())


def norm(ex, meta):
    out = dict(ex)
    out['supplier'] = canon(str(ex.get('supplier') or ''))
    out['doc_type'] = ex.get('doc_type') or 'invoice'
    try:
        out['amount'] = float(str(ex.get('amount_total')).replace(',', '')) if ex.get('amount_total') is not None else None
    except Exception:
        out['amount'] = None
    cur = str(ex.get('currency') or 'ILS').strip()
    out['currency'] = CUR.get(cur, cur.upper())
    d = pdate(ex.get('doc_date')) or pdate(meta.get('date'))
    out['doc_date'] = d.isoformat() if d else None
    out['doc_no'] = str(ex.get('doc_no')).strip() if ex.get('doc_no') not in (None, '') else None
    out['account_ref'] = str(ex.get('account_ref')).strip() if ex.get('account_ref') not in (None, '') else None
    return out


def series(hist, ex):
    recs = [h for h in hist if h.get('status') == 'filed' and h['supplier'] == ex['supplier'] and h.get('doc_type') == ex['doc_type'] and h.get('amount') is not None and h.get('doc_date')]
    if ex['account_ref']:
        same = [h for h in recs if h.get('account_ref') == ex['account_ref']]
        return same, recs
    return recs, recs


def fmt(x):
    return f'{x:,.2f}'.rstrip('0').rstrip('.') if isinstance(x, (int, float)) else str(x)


def evaluate(ex, hist, S):
    """returns (verdict, reasons, ref) ; verdict in ok|review|duplicate|rejected_dup"""
    sup = ex['supplier']
    for h in hist:
        if h['supplier'] != sup:
            continue
        same_no = ex['doc_no'] and h.get('doc_no') == ex['doc_no'] and h.get('doc_type') == ex['doc_type']
        same_val = (not ex['doc_no']) and h.get('doc_date') == ex['doc_date'] and ex['amount'] is not None \
            and h.get('amount') is not None and abs(h['amount'] - ex['amount']) < 0.01
        if same_no or same_val:
            return ('rejected_dup' if h.get('status') == 'rejected' else 'duplicate'), [f"כבר קיים (מס' {h.get('doc_no') or h.get('doc_date')})"], None
    reasons, ref = [], None
    if not sup or ex['amount'] is None or not ex['doc_date']:
        return 'review', ['לא הצלחתי לחלץ ספק, סכום או תאריך בוודאות'], None
    if ex['doc_type'] == 'credit' or ex['amount'] < 0:
        reasons.append('זיכוי או סכום שלילי')
    if ex['amount'] > S['high_amount']:
        reasons.append(f"סכום גבוה מעל {S['high_amount']}")
    ser, allrecs = series(hist, ex)
    ser = sorted(ser, key=lambda h: h['doc_date'])
    if not allrecs:
        reasons.append('ספק חדש, אין חשבוניות קודמות להשוואה')
    elif ex['account_ref'] and not ser:
        reasons.append(f"מזהה חשבון חדש אצל ספק קיים: {ex['account_ref']}")
    elif ser:
        last = ser[-1]
        ref = last
        if last.get('currency') and last['currency'] != ex['currency']:
            reasons.append(f"מטבע השתנה: {last['currency']} ל-{ex['currency']}")
        if ex['supplier'] in set(supplier_map().get('usage_based', [])):
            # amount follows usage (e.g. parking duration): no deviation check, other checks still apply
            fd0 = pdate(ex['doc_date'])
            if fd0 and (fd0 - datetime.date.today()).days > 7:
                reasons.append('תאריך עתידי')
            return ('review' if reasons else 'ok'), reasons, ref
        recent = ser[-3:]
        amts = [h['amount'] for h in recent]
        mean = sum(amts) / len(amts)
        fixed = len(ser) >= 3 and mean and all(abs(a - mean) / abs(mean) * 100 <= S['fixed_tol_pct'] for a in amts)
        d = pdate(ex['doc_date'])
        yoy = None
        for h in ser:
            hd = pdate(h['doc_date'])
            if d and hd and abs((d - hd).days - 365) <= 45:
                yoy = h
        if fixed:
            pct = (ex['amount'] - mean) / mean * 100
            if abs(pct) > S['fixed_tol_pct']:
                reasons.append(f"סכום קבוע השתנה: {fmt(ex['amount'])} לעומת {fmt(mean)} ({pct:+.1f}%)")
            ref = {'amount': mean, 'doc_date': last['doc_date'], 'label': 'סכום קבוע'}
        else:
            if yoy:
                base, label = yoy['amount'], 'אותה תקופה אשתקד'
            elif len(ser) >= 3:
                base, label = statistics.median(amts), 'חציון 3 אחרונות'
            else:
                base, label = sum(h['amount'] for h in ser) / len(ser), 'ממוצע קודמות'
            thr = S['variable_pct'] if len(ser) >= 3 else S['few_history_pct']
            if base:
                pct = (ex['amount'] - base) / abs(base) * 100
                if abs(pct) > thr and abs(ex['amount'] - base) > S['variable_min_abs']:
                    reasons.append(f"חריגה: {fmt(ex['amount'])} לעומת {fmt(base)} ({label}, {pct:+.0f}%)")
            ref = {'amount': base, 'doc_date': (yoy or last)['doc_date'], 'label': label}
        if ex['account_ref'] is None and last.get('account_ref'):
            pass
    fd = pdate(ex['doc_date'])
    if fd and (fd - datetime.date.today()).days > 7:
        reasons.append('תאריך עתידי')
    return ('review' if reasons else 'ok'), reasons, ref


def copy_files(d, meta, ex, target_dir):
    target_dir.mkdir(parents=True, exist_ok=True)
    files = [d / f for f in meta.get('files', []) if (d / f).exists()]
    if not files and (d / 'body.txt').exists():
        files = [d / 'body.txt']
    out = []
    base = f"{ex['doc_date']}_{safe(ex['doc_type'])}_{safe(ex['doc_no'] or meta['item_id'])}"
    for i, f in enumerate(files):
        suffix = f.suffix.lower() or '.bin'
        name = base + (f'_{i + 1}' if len(files) > 1 else '') + suffix
        shutil.copy2(f, target_dir / name)
        out.append(str(target_dir / name))
    return out


def hist_record(ex, meta, files, status, by, shas=None):
    return {'supplier': ex['supplier'], 'doc_type': ex['doc_type'], 'doc_no': ex['doc_no'], 'doc_date': ex['doc_date'],
            'period_from': ex.get('period_from'), 'period_to': ex.get('period_to'), 'amount': ex['amount'],
            'currency': ex['currency'], 'account_ref': ex['account_ref'], 'source': meta.get('source'),
            'item_id': meta['item_id'], 'message_id': meta.get('message_id'), 'files': files, 'status': status, 'by': by, 'shas': shas or [],
            'at': datetime.datetime.now().isoformat(timespec='seconds')}


def file_dir(ex):
    return FILED / ex['doc_date'][:4] / safe(ex['supplier'])


def run(baseline=False):
    st, hist = get_state(), load(HIST, [])
    S = st['settings']
    rep = {'filed': [], 'review': [], 'duplicate': [], 'ignored': [], 'errors': [], 'baseline': baseline, 'notified': False}
    items = []
    for d in sorted(INBOX.iterdir()):
        if not d.is_dir():
            continue
        meta = load(d / 'meta.json')
        if not meta:
            continue
        ex = load(d / 'extraction.json')
        if ex is None:
            meta['tries'] = meta.get('tries', 0) + 1
            if meta['tries'] >= 3:
                rep['errors'].append({'item': meta['item_id'], 'subject': meta.get('subject') or meta.get('caption'), 'error': 'חילוץ נכשל 3 פעמים'})
                shutil.move(str(d), str(FAILED / d.name))
            else:
                save(d / 'meta.json', meta)
            continue
        if not ex.get('is_invoice'):
            rep['ignored'].append(meta['item_id'])
            shutil.rmtree(d, ignore_errors=True)
            continue
        items.append((d, meta, norm(ex, meta)))
    items.sort(key=lambda t: t[2]['doc_date'] or '')
    for d, meta, ex in items:
        shas = [hashlib.md5((d / f).read_bytes()).hexdigest() for f in meta.get('files', []) if (d / f).exists()]
        verdict, reasons, ref = evaluate(ex, hist, S)
        if verdict not in ('duplicate', 'rejected_dup') and shas:
            for h in hist:
                if h['supplier'] == ex['supplier'] and set(shas) & set(h.get('shas', [])):
                    verdict, reasons = ('rejected_dup' if h.get('status') == 'rejected' else 'duplicate'), ['אותו קובץ בדיוק כמו מסמך קודם']
                    break
        label = {'supplier': ex['supplier'], 'doc_no': ex['doc_no'], 'doc_date': ex['doc_date'], 'amount': ex['amount'],
                 'currency': ex['currency'], 'item': meta['item_id'], 'source': meta.get('source'), 'account_ref': ex['account_ref']}
        if verdict in ('duplicate', 'rejected_dup'):
            rep['duplicate'].append(dict(label, reason=reasons[0], rejected=(verdict == 'rejected_dup')))
            shutil.rmtree(d, ignore_errors=True)
        elif verdict == 'ok' or baseline:
            files = copy_files(d, meta, ex, file_dir(ex))
            hist.append(hist_record(ex, meta, files, 'filed', 'baseline' if baseline else 'auto', shas))
            rep['filed'].append(dict(label, folder=str(file_dir(ex)), note=('' if verdict == 'ok' else '; '.join(reasons))))
            shutil.rmtree(d, ignore_errors=True)
        else:
            code = st['next_code']
            st['next_code'] += 1
            pdir = PENDING / str(code)
            files = copy_files(d, meta, ex, pdir)
            st['pending'][str(code)] = {'extraction': ex, 'meta': {k: meta.get(k) for k in ('item_id', 'source', 'subject', 'from', 'caption', 'chat', 'date', 'link', 'message_id')},
                                        'reasons': reasons, 'ref': ref, 'files': files, 'created': time.time()}
            rep['review'].append(dict(label, code=code, reasons=reasons, ref=ref, files=files, subject=meta.get('subject') or meta.get('caption')))
            hist.append(hist_record(ex, meta, files, 'pending', 'pending', shas))
            shutil.rmtree(d, ignore_errors=True)
    prev = load(REPORT, None)
    if prev and not prev.get('notified') and not prev.get('baseline') and not baseline:
        for k in ('filed', 'review', 'duplicate', 'errors'):
            rep[k] = prev.get(k, []) + rep[k]
    # history entries with status 'pending' are excluded from comparisons (status != filed) but still block duplicates
    save(HIST, hist)
    save(STATE, st)
    save(REPORT, rep)
    return rep


def approve(code):
    st, hist = get_state(), load(HIST, [])
    p = st['pending'].get(str(code))
    if not p:
        return f'#{code} לא נמצאה בין הממתינות'
    ex, meta = p['extraction'], p['meta']
    ex['doc_date'] = ex.get('doc_date') or (meta.get('date') or datetime.date.today().isoformat())
    ex['supplier'] = ex.get('supplier') or 'לא_מזוהה'
    target = file_dir(ex)
    target.mkdir(parents=True, exist_ok=True)
    moved = []
    for f in p['files']:
        fp = Path(f)
        if fp.exists():
            shutil.move(str(fp), str(target / fp.name))
            moved.append(str(target / fp.name))
    for h in hist:
        if h.get('item_id') == meta['item_id'] and h['status'] == 'pending':
            h.update(status='filed', by='user', files=moved, at=datetime.datetime.now().isoformat(timespec='seconds'))
    shutil.rmtree(PENDING / str(code), ignore_errors=True)
    del st['pending'][str(code)]
    save(HIST, hist)
    save(STATE, st)
    return f"#{code} אושרה ותויקה: {ex['supplier']} {ex['doc_date']} {fmt(ex['amount'])}"


def reject(code):
    st, hist = get_state(), load(HIST, [])
    p = st['pending'].get(str(code))
    if not p:
        return f'#{code} לא נמצאה בין הממתינות'
    meta, ex = p['meta'], p['extraction']
    rdir = REJECTED / str(code)
    rdir.mkdir(parents=True, exist_ok=True)
    for f in p['files']:
        if Path(f).exists():
            shutil.move(f, str(rdir / Path(f).name))
    for h in hist:
        if h.get('item_id') == meta['item_id'] and h['status'] == 'pending':
            h.update(status='rejected', by='user', files=[])
    shutil.rmtree(PENDING / str(code), ignore_errors=True)
    del st['pending'][str(code)]
    save(HIST, hist)
    save(STATE, st)
    return f"#{code} נדחתה: {ex['supplier']} {ex['doc_date']} {fmt(ex['amount'])} (הקובץ ב-rejected)"


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if cmd == 'run':
        r = run('--baseline' in sys.argv)
        print(json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in r.items()}, ensure_ascii=False))
    elif cmd == 'approve':
        print(approve(sys.argv[2]))
    elif cmd == 'reject':
        print(reject(sys.argv[2]))
    elif cmd == 'suppliers':
        seen = {}
        for h in load(HIST, []):
            if h.get('status') == 'filed':
                seen.setdefault(h['supplier'], set()).add(h.get('account_ref') or '')
        for lab in supplier_map().get('labels', {}):
            seen.setdefault(lab, set())
        for s, a in sorted(seen.items()):
            print(s + ((' | account_ref: ' + ', '.join(x for x in sorted(a) if x)) if any(a) else ''))
    else:
        st = get_state()
        print(json.dumps({'pending': {k: v['reasons'] for k, v in st['pending'].items()}, 'history': len(load(HIST, [])), 'inbox': len(list(INBOX.iterdir()))}, ensure_ascii=False))
