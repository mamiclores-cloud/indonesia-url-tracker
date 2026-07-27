"""Indo Shopee 連結管理 — 進入點。

雙擊此檔（pythonw 執行，無 cmd 視窗）：若服務已在跑則只開瀏覽器，
否則啟動 Flask + 背景 worker 並開啟瀏覽器。
除錯時可用 `python app.pyw` 於終端機執行看 log。
"""
import os
import sys
import threading
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tracker import config as cfg  # noqa: E402


def already_running(port):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


def main():
    cfg.load()
    port = cfg.get("port")
    url = f"http://127.0.0.1:{port}/products"
    if already_running(port):
        webbrowser.open(url)
        return
    from tracker import create_app
    app = create_app()
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
