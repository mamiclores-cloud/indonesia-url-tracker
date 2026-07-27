"""Minimal Chrome DevTools Protocol client over websocket-client.

Launches the user's real Chrome with a dedicated profile (Chrome 136+ refuses
remote debugging on the default profile) and drives one dedicated work tab.
"""
import json
import logging
import os
import subprocess
import threading
import time

import requests
import websocket

from . import config as cfg

log = logging.getLogger(__name__)


class CDPError(Exception):
    pass


class CDPClient:
    def __init__(self, ws_url):
        self.ws_url = ws_url
        self._ws = websocket.create_connection(ws_url, timeout=30, enable_multithread=True)
        self._id = 0
        self._id_lock = threading.Lock()
        self._pending = {}       # id -> {"event": Event, "result": ...}
        self._handlers = []      # (method, fn)
        self._closed = False
        self._pump = threading.Thread(target=self._pump_loop, daemon=True, name="cdp-pump")
        self._pump.start()

    def _pump_loop(self):
        while not self._closed:
            try:
                raw = self._ws.recv()
            except Exception:
                if not self._closed:
                    log.info("CDP websocket closed")
                self._closed = True
                for slot in self._pending.values():
                    slot["event"].set()
                return
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if "id" in msg:
                slot = self._pending.pop(msg["id"], None)
                if slot is not None:
                    slot["result"] = msg
                    slot["event"].set()
            else:
                method = msg.get("method", "")
                for m, fn in list(self._handlers):
                    if m == method:
                        try:
                            fn(msg.get("params", {}))
                        except Exception:
                            log.exception("CDP handler error for %s", method)

    def on(self, method, fn):
        self._handlers.append((method, fn))
        return fn

    def off(self, fn):
        self._handlers = [(m, f) for m, f in self._handlers if f is not fn]

    def send(self, method, params=None, timeout=30):
        if self._closed:
            raise CDPError("CDP connection closed")
        with self._id_lock:
            self._id += 1
            mid = self._id
        slot = {"event": threading.Event(), "result": None}
        self._pending[mid] = slot
        self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        if not slot["event"].wait(timeout):
            self._pending.pop(mid, None)
            raise CDPError(f"CDP timeout: {method}")
        if self._closed and slot["result"] is None:
            raise CDPError("CDP connection closed")
        res = slot["result"]
        if "error" in res:
            raise CDPError(f"{method}: {res['error']}")
        return res.get("result", {})

    def close(self):
        self._closed = True
        try:
            self._ws.close()
        except Exception:
            pass


# ------------------------------------------------------------ chrome mgmt --

def _http(port, path, method="GET"):
    url = f"http://127.0.0.1:{port}{path}"
    fn = requests.get if method == "GET" else requests.put
    return fn(url, timeout=5)


def chrome_alive(port=None):
    port = port or cfg.get("chrome_debug_port")
    try:
        return _http(port, "/json/version").status_code == 200
    except requests.RequestException:
        return False


def launch_chrome(extra_args=()):
    port = cfg.get("chrome_debug_port")
    os.makedirs(cfg.CHROME_PROFILE_DIR, exist_ok=True)
    args = [
        cfg.get("chrome_path"),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={cfg.CHROME_PROFILE_DIR}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-features=DefaultBrowserPrompt",
        *extra_args,
        "https://shopee.co.id/",
    ]
    log.info("launching chrome: %s", " ".join(args[:3]))
    subprocess.Popen(args, close_fds=True,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))


def ensure_chrome(timeout=40):
    """Return True when the debug endpoint is reachable, launching if needed."""
    port = cfg.get("chrome_debug_port")
    if chrome_alive(port):
        return True
    launch_chrome()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if chrome_alive(port):
            return True
        time.sleep(0.5)
    return False


def new_tab(url="about:blank"):
    port = cfg.get("chrome_debug_port")
    resp = _http(port, f"/json/new?{requests.compat.urlencode({'url': url})}", method="PUT")
    resp.raise_for_status()
    return resp.json()


def find_tab(target_id):
    port = cfg.get("chrome_debug_port")
    for t in _http(port, "/json").json():
        if t.get("id") == target_id:
            return t
    return None


def connect_tab(tab):
    ws_url = tab["webSocketDebuggerUrl"]
    try:
        return CDPClient(ws_url)
    except Exception as e:
        # Chrome may 403 the ws handshake depending on origin policy
        raise CDPError(f"無法附掛 Chrome 分頁: {e}")


def activate_tab(target_id):
    port = cfg.get("chrome_debug_port")
    try:
        _http(port, f"/json/activate/{target_id}")
    except requests.RequestException:
        pass
