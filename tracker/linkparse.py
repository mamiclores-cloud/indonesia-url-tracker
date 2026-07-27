"""Shopee URL parsing, canonicalization and short-URL resolution.

Correctness here gates both dedupe and sheet-row relocation, so keep the
extraction rules conservative: return None rather than guessing.
"""
import json
import logging
import re
import urllib.parse

import requests

from . import db

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

ITEM_PATH_RE = re.compile(r"i\.(\d{4,})\.(\d{6,})(?:[/?#]|$)")
PRODUCT_PATH_RE = re.compile(r"/product/(\d{4,})/(\d{6,})(?:[/?#]|$)")
META_REFRESH_RE = re.compile(
    r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+url=([^"\'>\s]+)', re.I)


def is_shopee(url: str) -> bool:
    try:
        host = urllib.parse.urlsplit(url).netloc.lower()
    except ValueError:
        return False
    return host == "shopee.co.id" or host.endswith(".shopee.co.id")


def parse_shopee_url(url: str):
    """Return dict(shopid, itemid, model_id) — model_id may be None."""
    if not url or not is_shopee(url):
        return None
    split = urllib.parse.urlsplit(url)
    m = ITEM_PATH_RE.search(split.path) or PRODUCT_PATH_RE.search(split.path)
    if not m:
        return None
    shopid, itemid = int(m.group(1)), int(m.group(2))
    model_id = None
    qs = urllib.parse.parse_qs(split.query)
    extra = qs.get("extraParams", [None])[0]
    if extra:
        try:
            model_id = json.loads(extra).get("display_model_id")
            if model_id is not None:
                model_id = int(model_id)
        except (ValueError, TypeError, AttributeError):
            model_id = None
    return {"shopid": shopid, "itemid": itemid, "model_id": model_id}


def canonical_url(shopid, itemid, model_id=None):
    base = f"https://shopee.co.id/product/{shopid}/{itemid}"
    if model_id:
        extra = urllib.parse.quote(json.dumps(
            {"display_model_id": int(model_id), "model_selection_logic": 3},
            separators=(",", ":")))
        return f"{base}?extraParams={extra}"
    return base


def dedupe_key(shopid, itemid):
    if shopid and itemid:
        return f"{shopid}.{itemid}"
    return ""


def resolve_url(url: str, use_cache=True, timeout=15):
    """Follow shortener redirects to a final URL. Returns final URL or None.

    Stages: cache -> requests redirects -> meta-refresh hops. A browser-based
    fallback lives in shopee.py (needs CDP). We trust resp.url even on 4xx —
    the redirect target is what we want, not the page body.
    """
    if is_shopee(url):
        return url
    if use_cache:
        row = db.q1("SELECT final_url FROM url_cache WHERE short_url=?", (url,))
        if row and row["final_url"]:
            return row["final_url"]
    final = None
    try:
        current = url
        for _ in range(4):
            resp = requests.get(current, headers={"User-Agent": UA},
                                timeout=timeout, allow_redirects=True)
            current = resp.url
            if is_shopee(current):
                break
            m = META_REFRESH_RE.search(resp.text[:20000])
            if not m:
                break
            nxt = urllib.parse.urljoin(current, m.group(1))
            if nxt == current:
                break
            current = nxt
        final = current
    except requests.RequestException as e:
        log.warning("resolve_url failed for %s: %s", url, e)
        return None
    if final and final != url:
        db.x("INSERT INTO url_cache(short_url, final_url, resolved_at) VALUES(?,?,?) "
             "ON CONFLICT(short_url) DO UPDATE SET final_url=excluded.final_url, resolved_at=excluded.resolved_at",
             (url, final, db.now()))
    return final


def cache_resolution(short_url: str, final_url: str):
    db.x("INSERT INTO url_cache(short_url, final_url, resolved_at) VALUES(?,?,?) "
         "ON CONFLICT(short_url) DO UPDATE SET final_url=excluded.final_url, resolved_at=excluded.resolved_at",
         (short_url, final_url, db.now()))


def classify_raw_url(url: str):
    """Cheap static classification without network: shopee | shortlink | other."""
    if is_shopee(url):
        return "shopee"
    row = db.q1("SELECT final_url FROM url_cache WHERE short_url=?", (url,))
    if row and row["final_url"] and is_shopee(row["final_url"]):
        return "shopee"
    host = urllib.parse.urlsplit(url).netloc.lower()
    if host in KNOWN_SHORTENERS:
        return "shortlink"
    return "other"


KNOWN_SHORTENERS = {
    "reurl.cc", "surl.li", "surli.cc", "surl.lu", "surl.gd", "surl.tv",
    "tinyurl.com", "rb.gy", "bit.ly", "is.gd", "cutt.ly", "s.id", "t.ly",
}
