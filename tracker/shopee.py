"""Shopee operations driven through the real Chrome via CDP.

We never issue signed API requests ourselves — we navigate real pages and read
the XHR responses the page makes (api/v4/pdp/get_pc, api/v4/search/search_items).
"""
import base64
import json
import logging
import os
import random
import threading
import time
import urllib.parse

import requests

from . import cdp
from . import config as cfg

log = logging.getLogger(__name__)

PDP_API = "/api/v4/pdp/get_pc"
SEARCH_API = "/api/v4/search/search_items"
CDN_BASE = "https://down-id.img.susercontent.com/file/"


class CaptchaDetected(Exception):
    pass


class LoginNeeded(Exception):
    pass


class PageTimeout(Exception):
    pass


class ShopeeDriver:
    def __init__(self):
        self.client = None
        self.target_id = None
        self._ops = 0
        self._lock = threading.RLock()
        self._captures = []  # active capture slots

    # ------------------------------------------------------------ plumbing --
    def ensure(self):
        with self._lock:
            if not cdp.ensure_chrome():
                raise cdp.CDPError("Chrome 無法啟動或 debug port 無法連線")
            if self.client and not self.client._closed and self.target_id:
                if cdp.find_tab(self.target_id):
                    return
                self.client.close()
                self.client = None
            tab = cdp.new_tab("about:blank")
            self.target_id = tab["id"]
            self.client = cdp.connect_tab(tab)
            self.client.send("Network.enable", {"maxResourceBufferSize": 50_000_000})
            self.client.send("Page.enable")
            self.client.on("Network.responseReceived", self._on_response)
            self.client.on("Network.loadingFinished", self._on_finished)

    def _on_response(self, params):
        url = params.get("response", {}).get("url", "")
        status = params.get("response", {}).get("status", 0)
        for cap in self._captures:
            if cap["pattern"] in url and cap["request_id"] is None:
                cap["request_id"] = params["requestId"]
                cap["status"] = status
                cap["url"] = url

    def _on_finished(self, params):
        # Runs on the CDP event-pump thread — must NOT issue a blocking CDP
        # command here (getResponseBody's reply is read by this very thread, so
        # calling it here deadlocks until timeout). Just signal the waiter; the
        # caller thread fetches the body itself.
        rid = params.get("requestId")
        for cap in self._captures:
            if cap.get("request_id") == rid and not cap["finished"]:
                cap["finished"] = True
                cap["event"].set()

    def _pace(self):
        p = cfg.get("pacing")
        self._ops += 1
        if p.get("long_pause_every") and self._ops % p["long_pause_every"] == 0:
            log.info("pacing long pause %ss", p["long_pause_s"])
            time.sleep(p["long_pause_s"])
        else:
            time.sleep(random.uniform(p["min_delay_s"], p["max_delay_s"]))

    def eval_js(self, expr, timeout=15):
        res = self.client.send("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True}, timeout=timeout)
        return res.get("result", {}).get("value")

    def current_state(self):
        try:
            return self.eval_js(
                "({href: location.href, title: document.title,"
                " body: (document.body && document.body.innerText || '').slice(0, 1500)})") or {}
        except cdp.CDPError:
            return {}

    def check_blocked(self, state=None):
        state = state or self.current_state()
        href = state.get("href", "")
        if "/verify/" in href or "captcha" in href.lower():
            raise CaptchaDetected(href)
        if "/buyer/login" in href or "/account/login" in href:
            raise LoginNeeded(href)
        return state

    def bring_to_front(self):
        if self.target_id:
            cdp.activate_tab(self.target_id)

    def navigate_capture(self, url, pattern, timeout=25):
        """Navigate the work tab and capture the first matching XHR body.

        The response body is fetched here on the caller thread (never inside
        the event handler) so the CDP pump thread stays free to read replies.
        """
        self.ensure()
        self._pace()
        cap = {"pattern": pattern, "request_id": None, "finished": False,
               "status": None, "url": None, "event": threading.Event()}
        self._captures.append(cap)
        try:
            self.client.send("Page.navigate", {"url": url})
            if not cap["event"].wait(timeout):
                state = self.check_blocked()
                raise PageTimeout(f"{pattern} 未出現；頁面={state.get('href','?')}")
            if cap["status"] == 403:
                self.bring_to_front()
                raise CaptchaDetected(f"API 403: {cap['url']}")
            try:
                body = self.client.send(
                    "Network.getResponseBody", {"requestId": cap["request_id"]}, timeout=20)
            except cdp.CDPError as e:
                raise PageTimeout(f"body 擷取失敗: {e}")
            text = body.get("body", "")
            if body.get("base64Encoded"):
                text = base64.b64decode(text).decode("utf-8", "replace")
            return json.loads(text)
        except CaptchaDetected:
            self.bring_to_front()
            raise
        finally:
            self._captures.remove(cap)

    # ------------------------------------------------------------ PDP -------
    def get_pdp(self, url):
        """Fetch product detail. Returns (parsed, raw_json)."""
        try:
            raw = self.navigate_capture(url, PDP_API)
        except PageTimeout:
            # retry once, then DOM fallback for taken-down pages. Blank out the
            # tab first so the SPA actually re-issues get_pc (navigating to the
            # same URL can be a no-op that never re-fires the XHR).
            try:
                self.client.send("Page.navigate", {"url": "about:blank"})
                time.sleep(0.5)
                raw = self.navigate_capture(url, PDP_API)
            except PageTimeout as e:
                state = self.check_blocked()
                body = (state.get("body") or "").lower()
                markers = ("tidak ditemukan", "tidak dapat ditemukan", "produk tidak ada",
                           "product not exist", "halaman yang anda cari tidak ada")
                if any(m in body for m in markers):
                    return ({"exists": False, "reason": "dom:not-found"}, None)
                raise e
        return (parse_pdp(raw), raw)

    def resolve_in_browser(self, url, timeout=25):
        """Last-resort shortener resolution: navigate and read location.href."""
        self.ensure()
        self._pace()
        self.client.send("Page.navigate", {"url": url})
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            time.sleep(1.5)
            state = self.current_state()
            href = state.get("href", "")
            if href and href not in ("about:blank", url):
                last = href
                if "shopee.co.id" in href:
                    break
        return last

    # ------------------------------------------------------------ search ----
    def search(self, keyword, location_filter=None, page=0):
        """location_filter: {"param": "locations"|"fe_filter_options", "value": str}
        as produced by record_locations()/finder._locations_for_tier(), or None."""
        q = {"keyword": keyword, "page": page}
        url = "https://shopee.co.id/search?" + urllib.parse.urlencode(q)
        if location_filter and location_filter.get("value"):
            url += f"&{location_filter['param']}=" + urllib.parse.quote(location_filter["value"])
        raw = self.navigate_capture(url, SEARCH_API, timeout=30)
        return (parse_search(raw), raw)

    # ------------------------------------------------------------ images ----
    def fetch_image(self, image_id):
        """Download a Shopee CDN image, cached on disk. Returns path or None."""
        if not image_id:
            return None
        path = os.path.join(cfg.IMAGE_DIR, f"{image_id}.jpg")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
        url = CDN_BASE + image_id
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://shopee.co.id/"},
                             timeout=20)
            if r.status_code == 200 and r.content:
                with open(path, "wb") as f:
                    f.write(r.content)
                return path
        except requests.RequestException:
            pass
        # fallback: fetch inside the browser (has cookies)
        try:
            self.ensure()
            b64 = self.eval_js(
                "fetch(%s).then(r=>r.blob()).then(b=>new Promise(res=>{"
                "const fr=new FileReader();fr.onload=()=>res(fr.result.split(',')[1]);"
                "fr.readAsDataURL(b);}))" % json.dumps(url), timeout=30)
            if b64:
                with open(path, "wb") as f:
                    f.write(base64.b64decode(b64))
                return path
        except Exception as e:
            log.warning("image fetch failed %s: %s", image_id, e)
        return None


# ------------------------------------------------------------ parsers ------

def _first(d, *keys):
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


def parse_pdp(raw):
    """Best-effort mapping of get_pc payload → normalized dict.

    Raw JSON is kept by callers in job_items.result_json so this mapping can
    be corrected offline against real captures (M2 calibration).
    """
    out = {"exists": True, "reason": None, "unlisted": False, "title": None,
           "shop_name": None, "images": [], "models": [], "item_status": None}
    if not isinstance(raw, dict):
        return {"exists": False, "reason": "no-json", "models": [], "images": []}
    err = raw.get("error")
    data = raw.get("data") or {}
    item = data.get("item") or {}
    if (err not in (0, None)) or not item:
        out["exists"] = False
        out["reason"] = f"api-error:{err}"
        return out
    out["title"] = _first(item, "title", "name")
    out["item_status"] = _first(item, "item_status", "status")
    status = str(out["item_status"] or "").lower()
    if status in ("banned", "deleted", "sip_deleted"):
        out["exists"] = False
        out["reason"] = f"item_status:{status}"
        return out
    # unlisted flag appears under different names across versions
    for k in ("is_unlisted", "unlisted", "is_prohibited", "is_delisted"):
        if item.get(k):
            out["unlisted"] = True
    imgs = _first(item, "images") or []
    if not imgs and item.get("image"):
        imgs = [item["image"]]
    out["images"] = imgs
    shop = data.get("shop_detailed") or data.get("shop") or {}
    out["shop_name"] = _first(shop, "name", "shop_name", "username") or item.get("shop_name")

    models = item.get("models") or []
    tier_imgs = {}
    for tv in item.get("tier_variations") or []:
        images = tv.get("images") or []
        for idx, im in enumerate(images):
            tier_imgs[idx] = im
    for m in models:
        price = _first(m, "price", "price_min")
        ext = m.get("extinfo") or {}
        tier_idx = (ext.get("tier_index") or [None])[0]
        out["models"].append({
            "model_id": _first(m, "model_id", "modelid", "id"),
            "name": (m.get("name") or "").strip(),
            "price_idr": int(price / 100000) if isinstance(price, (int, float)) and price else None,
            "stock": _model_stock(m),
            "in_stock": _model_in_stock(m),
            "image": m.get("image") or tier_imgs.get(tier_idx),
        })
    # single-model products sometimes ship an empty models list
    if not out["models"]:
        price = _first(item, "price", "price_min")
        out["models"].append({
            "model_id": None, "name": "-",
            "price_idr": int(price / 100000) if isinstance(price, (int, float)) and price else None,
            "stock": _model_stock(item), "in_stock": _model_in_stock(item, item_level=True),
            "image": imgs[0] if imgs else None,
        })
    return out


def _model_stock(m):
    """Numeric stock if Shopee still exposes it (mostly null nowadays)."""
    for k in ("stock", "normal_stock", "total_available_stock"):
        v = m.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    sinfo = m.get("stock_info") or {}
    for k in ("total_available_stock", "normal_stock", "stock"):
        v = sinfo.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return None


def _model_in_stock(m, item_level=False):
    """Availability as a tri-state bool. Shopee stopped exposing numeric stock,
    so prefer the boolean flags it does send (has_stock / is_grayout / status /
    stock_display). Returns True / False / None(unknown — treated as available
    by the checker so we never false-flag a live product as sold out)."""
    if isinstance(m.get("has_stock"), bool):
        return m["has_stock"]
    if isinstance(m.get("is_grayout"), bool):
        return not m["is_grayout"]
    n = _model_stock(m)
    if n is not None:
        return n > 0
    if item_level:
        sd = str(m.get("stock_display") or "").strip().lower()
        if sd:
            return "in stock" in sd or "stok" in sd
    return None


def parse_search(raw):
    """Normalize search_items payload → list of candidate dicts."""
    items = []
    if not isinstance(raw, dict):
        return items
    arr = raw.get("items")
    if arr is None and isinstance(raw.get("data"), dict):
        arr = raw["data"].get("items")
    for it in arr or []:
        b = it.get("item_basic") or it
        itemid = _first(b, "itemid", "item_id")
        shopid = _first(b, "shopid", "shop_id")
        if not itemid or not shopid:
            continue
        price = _first(b, "price", "price_min")
        items.append({
            "itemid": itemid, "shopid": shopid,
            "title": _first(b, "name", "title") or "",
            "price_idr": int(price / 100000) if isinstance(price, (int, float)) and price else None,
            "sold": _first(b, "historical_sold", "sold") or 0,
            "image": b.get("image"),
            "shop_location": b.get("shop_location") or "",
            "is_ad": bool(it.get("adsid")),
        })
    return items


# ------------------------------------------------------------ singleton ----

_driver = None
_driver_lock = threading.Lock()


def get_driver() -> ShopeeDriver:
    global _driver
    with _driver_lock:
        if _driver is None:
            _driver = ShopeeDriver()
        return _driver


def driver_status():
    alive = cdp.chrome_alive()
    return {"chrome_alive": alive, "work_tab": bool(_driver and _driver.target_id and alive)}


# -------------------------------------------- location filter recording ----

# Shopee's frontend has been observed using two different query param
# encodings for the same "Shipped From" filter on different sessions/tabs
# (older `locations=a,b,c` and newer `fe_filter_options=[{"group_name":
# "LOCATIONS","values":[...]}]`), so we detect and store whichever the live
# page actually sends rather than assuming one format.
LOCATION_PARAM_NAMES = ("fe_filter_options", "locations")

_recording = {"active": False, "result": None, "error": None}
_recording_lock = threading.Lock()


def record_locations(timeout=180):
    """Attach to the user's own Shopee search tab(s) and record whichever
    location-filter query param the next search_items XHR actually carries
    (fired when they CONFIRM the filter). Listens on every open search tab
    at once since we can't reliably tell which one the user is using.
    """
    with _recording_lock:
        if _recording["active"]:
            return
        _recording.update(active=True, result=None, error=None)

    def run():
        clients = []
        try:
            if not cdp.ensure_chrome():
                raise RuntimeError("Chrome 未啟動")
            tabs = [t for t in requests.get(
                        f"http://127.0.0.1:{cfg.get('chrome_debug_port')}/json", timeout=5).json()
                    if t.get("type") == "page" and "shopee.co.id/search" in t.get("url", "")]
            if not tabs:
                raise RuntimeError("找不到開著 shopee.co.id/search 的分頁，請先在 Chrome 搜尋任意關鍵字")
            found = threading.Event()

            def on_req(params):
                url = params.get("request", {}).get("url", "")
                if SEARCH_API not in url:
                    return
                qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                for name in LOCATION_PARAM_NAMES:
                    val = qs.get(name, [None])[0]
                    if val:
                        _recording["result"] = {"param": name, "value": val}
                        found.set()
                        return

            for t in tabs:
                try:
                    c = cdp.connect_tab(t)
                except cdp.CDPError as e:
                    log.warning("record_locations: 無法附掛分頁 %s: %s", t.get("id"), e)
                    continue
                c.send("Network.enable")
                c.on("Network.requestWillBeSent", on_req)
                clients.append(c)
            if not clients:
                raise RuntimeError("找到 shopee 搜尋分頁但全部附掛失敗，請確認 Chrome 是用「啟動 Chrome」開的")
            if not found.wait(timeout):
                raise RuntimeError(
                    "等候逾時：未偵測到位置篩選的搜尋請求。請確認錄製開始後才在 Chrome 重新套用篩選"
                    "（或換個關鍵字重新搜尋一次讓請求重新發出），且分頁數不要開太多，避免抓錯分頁")
            cfg.set_values({"search_locations_param": _recording["result"]})
        except Exception as e:
            _recording["error"] = str(e)
        finally:
            for c in clients:
                c.close()
            _recording["active"] = False

    threading.Thread(target=run, daemon=True, name="loc-record").start()


def recording_status():
    return dict(_recording)
