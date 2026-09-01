#!/usr/bin/env python3
"""HTTP client for orchestrator providers — POST JSON with retry on transient failures.

Extracted from orchestrator.py to keep provider/HTTP concerns isolated.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
def _http_post_json(url, headers, payload, timeout=120, retries=6):
    """POST JSON с ретраями на ТРАНЗИЕНТНЫЕ сбои (finding живого прогона: SSL-handshake timeout
    оборвал задачу). Ретраим сетевые таймауты/сбросы и 5xx/429 с бэкоффом; 4xx (кроме 429) —
    не ретраим (это не транзиент, а ошибка запроса/ключа). Бэкофф детерминированный (без сна на
    последней попытке)."""
    import json as _json
    import time
    import urllib.error
    import urllib.request
    body = _json.dumps(payload).encode("utf-8")
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body,
                                     headers={**headers, "content-type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:   # прокси — из env
                return _json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                raise
            # v3.0-rc7 (finding kimi): 429/overload перегруженного провайдера — уважаем Retry-After,
            # иначе экспон. бэкофф с бОльшим потолком (multi-call ENGINEERING-прогон переживает всплеск).
            ra = 0
            try:
                ra = int((e.headers or {}).get("Retry-After") or 0)
            except (ValueError, TypeError):
                ra = 0
            time.sleep(max(ra, min(3 * (2 ** attempt), 60)))
            continue
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e                                    # сетевой транзиент (в т.ч. SSL timeout)
            if attempt == retries - 1:
                raise
        time.sleep(min(3 * (2 ** attempt), 60))         # 3s,6,12,24,48,60 между попытками
    raise last if last else RuntimeError("http retry exhausted")


def main(argv):
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
