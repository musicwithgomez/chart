"""Starts the Goldmine bridge for a customer (started by START GOLDMINE.bat).

Plain Python on purpose: no PowerShell, no hidden windows, nothing that looks like a script trying to hide.
 - picks the first free port 8765-8769 (the website looks on all of them)
 - listens on this PC only (127.0.0.1), unless "allow_lan": true is set in config.json
 - opens the website's setup page once the bridge is up
"""
import json
import socket
import sys
import threading
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent


def free_port() -> int:
    for port in range(8765, 8770):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    print("Ports 8765-8769 are all in use. Close another Goldmine window and try again.")
    sys.exit(1)


def setup_url() -> str:
    try:
        lj = json.loads((HERE / "license.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return ""
    site = (lj.get("site") or lj.get("server") or "").split("#")[0].split("?")[0]
    if not site.startswith(("http://", "https://")):
        return ""
    # Just append "#setup", never a "/": ".../terminal_test.html", ".../terminal_test" (GitHub Pages without .html)
    # and ".../saas" (redirects to ".../saas/", the #setup part is kept) all open the right page this way.
    return site + "#setup"


def main():
    try:
        cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8-sig"))
    except Exception:
        cfg = {}
    host = "0.0.0.0" if cfg.get("allow_lan") else "127.0.0.1"
    port = free_port()
    if port != 8765:
        print(f"Port 8765 is used by another program; this bridge uses port {port}.")
    url = setup_url()
    if url:
        threading.Timer(4, lambda: webbrowser.open(url)).start()  # the website finds this bridge by itself
        print("Make sure MetaTrader 5 is open and logged in to your account.")
    print(f"Starting the Goldmine bridge on http://127.0.0.1:{port}  (keep this window open)")
    import uvicorn
    sys.path.insert(0, str(HERE))
    uvicorn.run("app:app", host=host, port=port)


if __name__ == "__main__":
    main()
