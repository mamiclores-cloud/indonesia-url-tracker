"""Offline CLI:  python -m tracker.cli <command>

  smoke          import all modules (Smart App Control smoke test)
  import-xlsx    mirror the local xlsx copy into SQLite and print stats
  stats          print mirror statistics
  keyword CODE   preview finder keywords for a product code
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def cmd_smoke():
    import flask, gspread, google.auth, PIL, websocket, requests, openpyxl  # noqa
    from . import (config, db, linkparse, sheets, cdp, shopee, imagesim,  # noqa
                   checker, finder, jobs, sheets_apply, web)
    import tracker  # noqa
    print("smoke OK — all modules import cleanly")
    print("flask", flask.__version__, "| gspread", gspread.__version__,
          "| Pillow", PIL.__version__, "| openpyxl", openpyxl.__version__)


def cmd_import_xlsx():
    from . import config as cfg, db, sheets
    cfg.load()
    db.init()
    res = sheets.import_xlsx()
    print(f"imported: {res['products']} products, {res['links']} links")
    cmd_stats()


def cmd_stats():
    from . import config as cfg, db
    cfg.load()
    db.init()
    n_prod = db.q1("SELECT COUNT(*) n FROM products")["n"]
    n_links = db.q1("SELECT COUNT(*) n FROM links WHERE active=1")["n"]
    n_direct = db.q1("SELECT COUNT(*) n FROM links WHERE active=1 AND raw_url LIKE '%shopee.co.id%'")["n"]
    n_ids = db.q1("SELECT COUNT(*) n FROM links WHERE active=1 AND dedupe_key!=''")["n"]
    n_model = db.q1("SELECT COUNT(*) n FROM links WHERE active=1 AND model_id IS NOT NULL")["n"]
    print(f"products: {n_prod}")
    print(f"active links: {n_links}")
    print(f"  direct shopee: {n_direct}")
    print(f"  with shopid.itemid: {n_ids}")
    print(f"  with display_model_id: {n_model}")
    dist = db.q("SELECT cnt, COUNT(*) n FROM (SELECT COUNT(*) cnt FROM links WHERE active=1 "
                "GROUP BY product_code) GROUP BY cnt ORDER BY cnt")
    for r in dist:
        print(f"  {r['cnt']} links: {r['n']} products")


def cmd_keyword(code):
    from . import config as cfg, db, finder
    cfg.load()
    db.init()
    rows = db.q("SELECT page_name, variant_text FROM links WHERE product_code=? AND active=1", (code,))
    for r in rows:
        print(f"C: {r['page_name'][:70]}")
        print(f"D: {r['variant_text']}")
        print(f"  → attempt1: {finder.build_keyword(r['page_name'], r['variant_text'], 1)}")
        print(f"  → attempt2: {finder.build_keyword(r['page_name'], r['variant_text'], 2)}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "smoke":
        cmd_smoke()
    elif cmd == "import-xlsx":
        cmd_import_xlsx()
    elif cmd == "stats":
        cmd_stats()
    elif cmd == "keyword" and len(sys.argv) > 2:
        cmd_keyword(" ".join(sys.argv[2:]))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
