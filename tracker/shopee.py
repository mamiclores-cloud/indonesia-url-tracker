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
            if cap["pattern"] in url and cap["response"] is None:
                cap["request_id"] = params["requestId"]
                cap["status"] = status
                cap["url"] = url

    def _on_finished(self, params):
        rid = params.get("requestId")
        for cap in self._captures:
            if cap.get("request_id") == rid and cap["response"] is None:
                try:
                    body = self.client.send("Network.getResponseBody", {"requestId": rid}, timeout=20)
                    text = body.get("body", "")
                    if body.get("base64Encoded"):
                        text = base64.b64decode(text).decode("utf-8", "replace")
                    cap["response"] = text
                except Exception as e:
                    cap["error"] = str(e)
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
        """Navigate the work tab and capture the first matching XHR body."""
        self.ensure()
        self._pace()
        cap = {"pattern": pattern, "request_id": None, "response": None,
               "error": None, "status": None, "url": None,
               "event": threading.Event()}
        self._captures.append(cap)
        try:
            self.client.send("Page.navigate", {"url": url})
            if not cap["event"].wait(timeout):
                state = self.check_blocked()
                raise PageTimeout(f"{pattern} 未出現；頁面={state.get('href','?')}")
            if cap["error"]:
                raise PageTimeout(f"body 擷取失敗: {cap['error']}")
            if cap["status"] == 403:
                self.bring_to_front()
                raise CaptchaDetected(f"API 403: {cap['url']}")
            return json.loads(cap["response"])
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
            # retry once, then DOM fallback for taken-down pages
            try:
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
    def search(self, keyword, locations_param="", page=0):
        q = {"keyword": keyword, "page": page}
        url = "https://shopee.co.id/search?" + urllib.parse.urlencode(q)
        if locations_param:
            url += "&locations=" + urllib.parse.quote(locations_param)
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
        stock = m.get("stock")
        if stock is None:
            stock = _first(m, "normal_stock", "total_available_stock")
        if stock is None:
            sinfo = m.get("stock_info") or {}
            stock = _first(sinfo, "total_available_stock", "normal_stock", "stock")
        ext = m.get("extinfo") or {}
        tier_idx = (ext.get("tier_index") or [None])[0]
        out["models"].append({
            "model_id": _first(m, "model_id", "modelid", "id"),
            "name": (m.get("name") or "").strip(),
            "price_idr": int(price / 100000) if isinstance(price, (int, float)) and price else None,
            "stock": stock,
            "image": m.get("image") or tier_imgs.get(tier_idx),
        })
    # single-model products sometimes ship an empty models list
    if not out["models"]:
        price = _first(item, "price", "price_min")
        stock = _first(item, "stock", "normal_stock")
        out["models"].append({
            "model_id": None, "name": "-",
            "price_idr": int(price / 100000) if isinstance(price, (int, float)) and price else None,
            "stock": stock, "image": imgs[0] if imgs else None,
        })
    return out


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

_recording = {"active": False, "result": None, "error": None}


def record_locations(timeout=180):
    """Attach to the user's own Shopee search tab and record the `locations=`
    param from the next search_items XHR (fired when they CONFIRM the filter).
    """
    _recording.update(active=True, result=None, error=None)

    def run():
        client = None
        try:
            if not cdp.ensure_chrome():
                raise RuntimeError("Chrome 未啟動")
            tab = None
            for t in requests.get(
                    f"http://127.0.0.1:{cfg.get('chrome_debug_port')}/json", timeout=5).json():
                if t.get("type") == "page" and "shopee.co.id/search" in t.get("url", ""):
                    tab = t
                    break
            if not tab:
                raise RuntimeError("找不到開著 shopee.co.id/search 的分頁，請先在 Chrome 搜尋任意關鍵字")
            client = cdp.connect_tab(tab)
            client.send("Network.enable")
            found = threading.Event()

            def on_req(params):
                url = params.get("request", {}).get("url", "")
                if SEARCH_API in url:
                    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                    loc = qs.get("locations", [None])[0]
                    if loc:
                        _recording["result"] = loc
                        found.set()

            client.on("Network.requestWillBeSent", on_req)
            if not found.wait(timeout):
                raise RuntimeError("等候逾時：未偵測到帶 locations 參數的搜尋請求")
            cfg.set_values({"search_locations_param": _recording["result"]})
        except Exception as e:
            _recording["error"] = str(e)
        finally:
            if client:
                client.close()
            _recording["active"] = False

    threading.Thread(target=run, daemon=True, name="loc-record").start()


def recording_status():
    return dict(_recording)
