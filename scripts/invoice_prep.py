#!/usr/bin/env python3
"""Pre-extract text from PDFs in the inbox (pypdf) so the LLM step does not depend on PDF rendering."""
import json
from invoice_common import *


def main():
    from pypdf import PdfReader
    done = empty = 0
    for d in sorted(INBOX.iterdir()):
        meta = load(d / 'meta.json') if d.is_dir() else None
        if not meta:
            continue
        for f in meta.get('files', []):
            p = d / f
            out = d / (f + '.txt')
            if p.suffix.lower() != '.pdf' or out.exists() or not p.exists():
                continue
            txt = ''
            try:
                r = PdfReader(str(p))
                txt = '\n'.join((pg.extract_text() or '') for pg in r.pages[:8])[:12000]
            except Exception as e:
                txt = ''
            out.write_text(txt.strip(), 'utf-8')
            if not txt.strip():
                try:
                    r = PdfReader(str(p))
                    n = 0
                    for pg in r.pages[:3]:
                        for im in pg.images[:1]:
                            n += 1
                            jp = d / f'{f}.page{n}.jpg'
                            try:
                                from PIL import Image
                                import io
                                img = Image.open(io.BytesIO(im.data)).convert('RGB')
                                img.thumbnail((1800, 1800))
                                img.save(jp, 'JPEG', quality=80)
                            except Exception:
                                if len(im.data) < 4_000_000:
                                    jp.write_bytes(im.data)
                except Exception:
                    pass
            done += 1
            empty += 0 if txt.strip() else 1
    print(json.dumps({'pdfs_processed': done, 'no_text': empty}))


if __name__ == '__main__':
    main()
