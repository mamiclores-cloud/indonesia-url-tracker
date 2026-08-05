"""Shopee operations driven through the real Chrome via CDP.

We never issue signed API requests ourselves — we navigate real pages and read
the XHR responses the page makes (api/v4/pdp/get_pc, api/v4/search/search_items).
"""
import base64
import datetime
import json
import logging
import os
import random
import re
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


# --------------------------------------------------- 回應形狀的白名單 ------
# Shopee 商品 API 正常回應的信封長這樣（成功與業務錯誤共用）：
#   {"bff_meta": ..., "error": null|<code>, "error_msg": ..., "data": {...}|null}
# 反機器人系統攔截時回的是**完全不同的東西**（全數字鍵、帶挑戰碼、連 data
# 這個鍵都沒有）：
#   {"5": false, "2": false, "0": 2, "3": 90309999, "error": 90309999,
#    "1": "<challenge-id>", "9": true}
# 因此預設立場是「看不懂的回應不下任何業務判定」——絕不可以把攔截解讀成
# 「商品不存在」，那會把好連結標成失效並寫進客戶的表（0803 事故成因）。
BLOCK_ERROR_CODES = {90309999}       # 已知的反機器人攔截碼，不需再確認
KNOWN_NOT_FOUND_CODES = {266900002}  # 已知代表「商品真的不存在」

# 連續幾次搜尋回 0 筆就觸發主動確認。實測健康期 2415 次 tier 級搜尋只有 3 次
# 回 0 筆、最長連續 1 次；被擋時則是連續數百次全 0。
ZERO_RESULT_TRIGGER = 3

PROBE_INTERVAL_S = 600      # 被擋後每 10 分鐘探測一次
PROBE_MAX_FAILS = 12        # 連續失敗 12 次（2 小時）就停止探測，純等人
BLOCK_WARN_COUNT = 3        # 今日被擋達此次數 → UI 警告該檢查帳號

# 驗證碼頁面的可見文字（image5.png 那種挑戰會直接長在商品網址上，
# 沒有 /verify/ 可認，只能靠 DOM）
CAPTCHA_MARKERS = ("verify to continue", "slide to complete the puzzle",
                   "please slide", "geser untuk", "silakan geser", "verifikasi")


# ------------------------------------------------------- 全域封鎖狀態 ------
# 兩個 worker（Shopee / Sheet）都讀這裡：一旦被擋，整個系統一起停手，
# 等人解驗證碼或等探測通過，期間不寫任何判定。
_block = {"blocked": False, "since": None, "reason": None, "probe_fails": 0,
          "day": None, "count_today": 0}
_block_lock = threading.Lock()

# 長休（long pause / 工作區塊休息）期間使用者按暫停或停止，不該等好幾分鐘
# 才有反應，所以所有等待都走這個可中斷的 sleep。
_interrupt = threading.Event()


def interrupt_waits():
    _interrupt.set()


def clear_interrupt():
    _interrupt.clear()


def _sleep(seconds):
    if seconds > 0:
        _interrupt.wait(seconds)


def mark_blocked(reason):
    """記錄「被擋」。同一段封鎖只計一次，直到 clear_blocked() 為止。"""
    with _block_lock:
        today = datetime.date.today().isoformat()
        if _block["day"] != today:
            _block.update(day=today, count_today=0)
        if not _block["blocked"]:
            _block.update(blocked=True, probe_fails=0, reason=str(reason)[:300],
                          since=datetime.datetime.now().isoformat(timespec="seconds"),
                          count_today=_block["count_today"] + 1)
            log.warning("偵測到封鎖（今日第 %s 次）：%s", _block["count_today"], reason)


def clear_blocked():
    with _block_lock:
        if _block["blocked"]:
            log.info("封鎖解除（自 %s 起）", _block["since"])
        _block.update(blocked=False, since=None, reason=None, probe_fails=0)


def is_blocked():
    return _block["blocked"]


def block_status():
    with _block_lock:
        return dict(_block)


def note_probe_failure():
    with _block_lock:
        _block["probe_fails"] += 1
        return _block["probe_fails"]


def _raise_blocked(reason):
    mark_blocked(reason)
    raise CaptchaDetected(reason)


class ShopeeDriver:
    def __init__(self):
        self.client = None
        self.target_id = None
        self._ops = 0
        self._lock = threading.RLock()
        self._captures = []  # active capture slots
        self._work_since = None   # 本輪連續工作起點（給工作區塊休息用）
        self._zero_streak = 0     # 連續回 0 筆的搜尋次數

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

    def _work_break(self):
        """每連續工作 N 小時強制休息 M 分鐘。

        0803 事故的兩個變因裡，「連續運轉時長」是從未被驗證過的那一個：舊節奏
        只跑過 22 小時且零驗證碼，本次在第 29.6 小時被擋。放慢節奏無法取代
        休息——長時間不間斷的導覽本身就是被標記的主因之一。
        """
        hours = float(cfg.get("work_block_hours") or 0)
        mins = float(cfg.get("work_break_minutes") or 0)
        if hours <= 0 or mins <= 0:
            return
        now = time.monotonic()
        if self._work_since is None:
            self._work_since = now
            return
        if now - self._work_since >= hours * 3600:
            log.info("連續工作 %.1f 小時，強制休息 %.0f 分鐘",
                     (now - self._work_since) / 3600.0, mins)
            _sleep(mins * 60)
            self._work_since = time.monotonic()

    def _pace(self):
        p = cfg.get("pacing")
        self._work_break()
        self._ops += 1
        if p.get("long_pause_every") and self._ops % p["long_pause_every"] == 0:
            log.info("pacing long pause %ss", p["long_pause_s"])
            _sleep(p["long_pause_s"])
        else:
            _sleep(random.uniform(p["min_delay_s"], p["max_delay_s"]))

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

    def captcha_in_dom(self, state=None):
        """驗證碼是否直接長在當前頁面上——不看網址。

        實際遇到的挑戰（image5.png）出現在 /product/<shop>/<item> 上，網址
        完全正常、只有頁面內容是驗證框，光靠 href 判斷必然漏掉。
        """
        state = state if state is not None else self.current_state()
        text = ((state.get("body") or "") + " " + (state.get("title") or "")).lower()
        return any(m in text for m in CAPTCHA_MARKERS)

    def check_blocked(self, state=None):
        state = state if state is not None else self.current_state()
        href = state.get("href", "")
        if "/verify/" in href or "captcha" in href.lower():
            _raise_blocked(f"驗證頁面：{href}")
        if "/buyer/login" in href or "/account/login" in href:
            mark_blocked(f"要求登入：{href}")
            raise LoginNeeded(href)
        if self.captcha_in_dom(state):
            _raise_blocked(f"頁面出現驗證碼：{href}")
        return state

    def probe(self):
        """主動確認系統還能不能正常取得資料。回傳 True＝正常、False＝仍被擋。

        判定（連續 0 結果／看不懂的回應／body 被清掉）、被擋後的定期探測、
        以及恢復認定，三處共用這一支：只有一套封鎖認定，不會出現「這邊說被擋、
        那邊說沒被擋」的矛盾。

        測試對象從 DB 現挑一條最近確認還活著的連結，而不是寫死網址——寫死的
        商品總有一天會下架，之後探測永遠失敗，系統就再也醒不過來。

        本函式自己不丟例外（呼叫端要的是「是或否」，不是另一個要處理的錯誤）。
        """
        from . import db
        row = db.q1("SELECT canonical_url FROM links WHERE active=1 AND status='valid' "
                    "AND canonical_url IS NOT NULL AND canonical_url!='' "
                    "ORDER BY last_checked_at DESC LIMIT 1")
        if row is None:
            log.warning("probe：DB 裡沒有可用的 valid 連結，無從確認，視為正常")
            return True
        try:
            raw = self.navigate_capture(row["canonical_url"], PDP_API,
                                        timeout=25, pace=False, confirm=False)
        except (CaptchaDetected, LoginNeeded):
            return False
        except (PageTimeout, cdp.CDPError) as e:
            log.info("probe 失敗：%s", e)
            return False
        if not _pdp_envelope_ok(raw):
            return False
        err = raw.get("error")
        return err in (0, None) or err in KNOWN_NOT_FOUND_CODES

    def bring_to_front(self):
        if self.target_id:
            cdp.activate_tab(self.target_id)

    def navigate_capture(self, url, pattern, timeout=25, pace=True, confirm=True):
        """Navigate the work tab and capture the first matching XHR body.

        The response body is fetched here on the caller thread (never inside
        the event handler) so the CDP pump thread stays free to read replies.

        confirm=False 供 probe() 使用，避免探測自己再遞迴觸發一次探測。
        """
        self.ensure()
        if pace:
            self._pace()
        cap = {"pattern": pattern, "request_id": None, "finished": False,
               "status": None, "url": None, "event": threading.Event()}
        self._captures.append(cap)
        try:
            self.client.send("Page.navigate", {"url": url})
            if not cap["event"].wait(timeout):
                # XHR 根本沒發出，最常見的成因就是被導去驗證頁
                state = self.check_blocked()
                raise PageTimeout(f"{pattern} 未出現；頁面={state.get('href','?')}")
            if cap["status"] == 403:
                _raise_blocked(f"API 403: {cap['url']}")
            try:
                body = self.client.send(
                    "Network.getResponseBody", {"requestId": cap["request_id"]}, timeout=20)
            except cdp.CDPError as e:
                # -32000「No resource with given identifier found」＝ XHR 發出後
                # 頁面被導走、resource 被清掉，正是攔截的典型足跡（0803 那批
                # 失敗有 125 次是這個）。先確認系統狀態再決定怎麼歸類。
                if confirm and not self.probe():
                    _raise_blocked(f"body 擷取失敗且系統確認被擋：{e}")
                raise PageTimeout(f"body 擷取失敗: {e}")
            text = body.get("body", "")
            if body.get("base64Encoded"):
                text = base64.b64decode(text).decode("utf-8", "replace")
            try:
                return json.loads(text)
            except ValueError:
                _raise_blocked(f"{pattern} 回應不是 JSON（疑似攔截頁）")
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
        return (self._parse_pdp_guarded(raw), raw)

    def _parse_pdp_guarded(self, raw):
        """把「看不懂的回應」擋在業務判定之外。

        舊行為是 error 不為 0/None 就一律判 exists=False → 連結被標失效 →
        排隊把「link error」寫進客戶的表。0803 的攔截碼 90309999 就是這樣把
        5 條好連結標成失效的。現在預設反過來：只有認得的回應才下判定。
        """
        if not _pdp_envelope_ok(raw):
            _raise_blocked("商品 API 回應不是 Shopee 的格式（疑似反機器人攔截）")
        err = raw.get("error")
        if err in BLOCK_ERROR_CODES:
            _raise_blocked(f"商品 API 攔截碼 {err}")
        if err not in (0, None) and err not in KNOWN_NOT_FOUND_CODES:
            # 沒看過的錯誤碼：先確認是不是系統被擋；不是的話也不下定論，
            # 交給人複核（checker 會標 error 且不排任何 Sheet 寫入）
            if not self.probe():
                _raise_blocked(f"未知錯誤碼 {err} 且系統確認被擋")
            log.warning("未知的商品 API 錯誤碼 %s，不下判定交人複核", err)
            return {"exists": False, "reason": f"api-error:{err}", "unknown_error": True,
                    "unlisted": False, "models": [], "images": []}
        return parse_pdp(raw)

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
        if last is None or "shopee.co.id" not in (last or ""):
            self.check_blocked()   # 追不到蝦皮網址時，先排除「其實是被擋住」
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
        if not _search_envelope_ok(raw):
            err = raw.get("error") if isinstance(raw, dict) else None
            if err in BLOCK_ERROR_CODES:
                _raise_blocked(f"搜尋被攔截（error={err}）")
            if not self.probe():
                _raise_blocked("搜尋回應格式無法辨識，且系統確認被擋")
            raise PageTimeout(f"搜尋回應格式無法辨識：{str(raw)[:150]}")
        results = parse_search(raw)
        self._note_search_results(len(results))
        return (results, raw)

    def _note_search_results(self, n):
        """連續 0 筆是軟封鎖唯一的訊號——HTTP 200、格式正確、就是沒東西。

        但「關鍵字爛」「位置篩選太窄」「商品絕版」長得一模一樣，所以計數器
        只當觸發器，判定交給 probe()。實測健康期 2415 次搜尋最長連續 0 筆
        只有 1 次，被擋時則是連續數百次。
        """
        if n:
            self._zero_streak = 0
            return
        self._zero_streak += 1
        if self._zero_streak < ZERO_RESULT_TRIGGER:
            return
        log.info("連續 %s 次搜尋回 0 筆，主動確認系統狀態", self._zero_streak)
        if self.probe():
            self._zero_streak = 0      # 系統正常 → 是關鍵字的問題，不是被擋
        else:
            _raise_blocked(f"連續 {self._zero_streak} 次搜尋回 0 筆且系統確認被擋")

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


def _pdp_envelope_ok(raw):
    """這是不是商品 API 本人回的？（成功與業務錯誤共用同一個信封）

    正常：{"bff_meta":…, "error": null|<code>, "error_msg":…, "data": {…}|null}
    攔截：{"5":false, "2":false, "0":2, "3":90309999, "error":90309999,
           "1":"<challenge-id>", "9":true}   ← 連 data 這個鍵都沒有
    """
    return isinstance(raw, dict) and "data" in raw and "error" in raw


def _search_envelope_ok(raw):
    """搜尋 API 跨版本有兩種佈局：頂層 items，或 data.items。"""
    if not isinstance(raw, dict):
        return False
    if "items" in raw:
        return True
    d = raw.get("data")
    return isinstance(d, dict) and "items" in d


def parse_pdp(raw):
    """Best-effort mapping of get_pc payload → normalized dict.

    Raw JSON is kept by callers in job_items.result_json so this mapping can
    be corrected offline against real captures (M2 calibration).
    """
    out = {"exists": True, "reason": None, "unlisted": False, "title": None,
           "shop_name": None, "brand": None, "images": [], "models": [], "item_status": None,
           "shop_location": None, "historical_sold": None}
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
    brand = item.get("brand") or (item.get("global_brand") or {}).get("display_name")
    out["brand"] = brand.strip() if isinstance(brand, str) and brand.strip() else None
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
    # 地區/銷量（UI 正式連結區欄位）— key 名跨版本不定，一律 best-effort
    loc = _first(shop, "shop_location", "place", "region") or item.get("shop_location")
    out["shop_location"] = loc.strip() if isinstance(loc, str) and loc.strip() else None
    sold = _first(item, "historical_sold", "global_sold", "sold")
    if isinstance(sold, str):
        sold = _sold_to_int(sold)
    out["historical_sold"] = int(sold) if isinstance(sold, (int, float)) else None

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


def _sold_to_int(text):
    """Parse Shopee sold strings like '10RB+ sold', '1,2RB', '56 sold'. RB=1000."""
    if not text:
        return None
    t = str(text).lower().replace("terjual", "").replace("sold", "").strip()
    m = re.search(r"([\d.,]+)\s*(rb|k|jt)?", t)
    if not m:
        return None
    num = m.group(1).replace(".", "").replace(",", ".")
    try:
        val = float(num)
    except ValueError:
        return None
    mult = {"rb": 1000, "k": 1000, "jt": 1_000_000}.get(m.group(2), 1)
    return int(val * mult)


def parse_search(raw):
    """Normalize search_items payload → list of candidate dicts.

    Handles the current Shopee response (data spread across item_data +
    item_card_displayed_asset) and the legacy item_basic layout.
    """
    items = []
    if not isinstance(raw, dict):
        return items
    arr = raw.get("items")
    if arr is None and isinstance(raw.get("data"), dict):
        arr = raw["data"].get("items")
    for it in arr or []:
        legacy = it.get("item_basic")          # old layout (may be None now)
        data = it.get("item_data") or {}       # new structured data
        asset = it.get("item_card_displayed_asset") or {}  # new name/image/location
        b = legacy or {}

        itemid = _first(b, "itemid", "item_id") or data.get("itemid") or it.get("itemid")
        shopid = _first(b, "shopid", "shop_id") or data.get("shopid") or it.get("shopid")
        if not itemid or not shopid:
            continue

        # price (micro-units → IDR); prefer new structured, fall back to legacy
        price = None
        for src in (data.get("item_card_display_price"), asset.get("display_price")):
            if isinstance(src, dict) and isinstance(src.get("price"), (int, float)):
                price = src["price"]
                break
        if price is None:
            price = _first(b, "price", "price_min")

        # sold count
        sold = None
        sc = data.get("item_card_display_sold_count") or {}
        if isinstance(sc.get("historical_sold_count"), (int, float)):
            sold = int(sc["historical_sold_count"])
        elif sc.get("historical_sold_count_text"):
            sold = _sold_to_int(sc["historical_sold_count_text"])
        elif asset.get("sold_count", {}).get("text"):
            sold = _sold_to_int(asset["sold_count"]["text"])
        if sold is None:
            sold = _first(b, "historical_sold", "sold")

        shop_data = data.get("shop_data") or {}
        is_ad = bool(it.get("adsid")) or (asset.get("icon_in_image") or {}).get("ads_text") == "Ad"

        items.append({
            "itemid": itemid, "shopid": shopid,
            "title": asset.get("name") or _first(b, "name", "title") or "",
            "price_idr": int(price / 100000) if isinstance(price, (int, float)) and price else None,
            "sold": sold or 0,
            "image": asset.get("image") or b.get("image"),
            "shop_location": asset.get("shop_location") or b.get("shop_location") or "",
            "shop_name": shop_data.get("shop_name") or "",
            "is_sold_out": bool(data.get("is_sold_out")) if "is_sold_out" in data else None,
            "is_ad": is_ad,
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


def probe_now():
    """給探測執行緒用的模組層入口（不會因為 driver 尚未建立而爆掉）。"""
    try:
        return get_driver().probe()
    except Exception as e:      # Chrome 沒開、CDP 掛掉等一律視為仍被擋
        log.info("probe_now 失敗：%s", e)
        return False


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
