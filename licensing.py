"""License check for the bridge (customer side).

OFF by default: with no license.json (and no GOMEZ_LICENSE_* settings) the bridge runs exactly as before,
so YOUR own bridge keeps working. A customer bridge gets license.json inside the download from the license server:
    {"server": "https://app.yourdomain.com", "pubkey": "<hex>", "key": "GP-XXXXX-..."}

How it works
- Every 10 minutes the bridge asks the license server "is this key active?".
- The answer is signed with the server's private key; the bridge checks it with the public key (gp_sign.py),
  so editing the answer or pointing the bridge to a fake server does not work.
- The last good answer is cached in license_cache.json. If the server is unreachable the bridge keeps working
  until that answer expires (72 h by default), so a short outage never stops your customers from trading.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import gp_sign

VERSION = "1.0.0"

# Optional: hard-code YOUR license server public key here before you ship the bridge.
# Then a customer cannot point the bridge at another server with another key.
PINNED_PUBKEY = ""


class License:
    def __init__(self, base: Path, config: dict):
        self.base = base
        self.file = base / "license.json"
        self.cache = base / "license_cache.json"
        f = {}
        if self.file.exists():
            try:
                f = json.loads(self.file.read_text(encoding="utf-8"))
            except Exception as e:
                print("[license] license.json is not valid JSON:", e)
        lc = config.get("license") or {}
        self.server = (os.getenv("GOMEZ_LICENSE_SERVER") or lc.get("server") or f.get("server") or "").rstrip("/")
        self.key = (os.getenv("GOMEZ_LICENSE_KEY") or lc.get("key") or f.get("key") or "").strip().upper()
        pub = PINNED_PUBKEY or os.getenv("GOMEZ_LICENSE_PUBKEY") or lc.get("pubkey") or f.get("pubkey") or ""
        self.pub = bytes.fromhex(pub) if pub else b""
        self.enabled = bool(self.server)  # licensing is active when a license server is configured
        self.machine = hashlib.sha256(f"{uuid.getnode()}|{platform.node()}|goldmine".encode()).hexdigest()[:32]
        self.data: dict = {}
        self.error = ""
        self.checked = 0.0
        self._lock = threading.Lock()
        if self.cache.exists():
            try:
                self._accept(json.loads(self.cache.read_text())["token"], save=False)
            except Exception:
                pass
        if self.enabled:
            print(f"[license] licensed mode - server {self.server} - key {self.key[:8]}... - PC id {self.machine[:8]}")
            threading.Thread(target=self._loop, daemon=True).start()

    # ---------------------------------------------------------------- state
    def ok(self) -> bool:
        if not self.enabled:
            return True
        d = self.data
        return bool(d.get("valid") and d.get("exp", 0) > time.time() and d.get("machine") == self.machine and d.get("key") == self.key)

    def reason(self) -> str:
        if not self.key:
            return "No license key yet. Open the web app > Dashboard > Connect your bridge, and activate it."
        if self.data.get("reason"):
            return self.data["reason"]
        if self.data and self.data.get("exp", 0) <= time.time():
            return "The license could not be confirmed for too long. Check the internet connection of this PC."
        return self.error or "Checking the license…"

    def status(self) -> dict:
        d = self.data
        return {"enabled": self.enabled, "ok": self.ok(), "reason": "" if self.ok() else self.reason(),
                "plan": d.get("plan"), "status": d.get("status"), "features": d.get("features", []),
                "limits": d.get("limits", {}), "email": d.get("email"), "period_end": d.get("period_end"),
                "valid_until": d.get("exp"), "checked": self.checked, "server": self.server,
                "key": (self.key[:8] + "…") if self.key else "", "machine": self.machine[:8], "version": VERSION}

    def feature(self, name: str) -> bool:
        return not self.enabled or (self.ok() and name in self.data.get("features", []))

    # ---------------------------------------------------------------- server
    def _accept(self, token: str, save=True):
        if not self.pub:
            raise ValueError("No public key configured for the license server.")
        d = gp_sign.verify_token(token, self.pub)
        if d.get("machine") != self.machine:
            raise ValueError("License answer is for another PC.")
        self.data = d
        if save:
            self.cache.write_text(json.dumps({"token": token}))

    def refresh(self) -> dict:
        with self._lock:
            if not self.enabled or not self.key:
                return self.status()
            if not self.pub:  # first contact without a pinned key: fetch it once and keep it (trust on first use)
                try:
                    with urllib.request.urlopen(self.server + "/api/license/pubkey", timeout=15) as r:
                        self.pub = bytes.fromhex(json.loads(r.read())["pubkey"])
                    self._save_file(pubkey=self.pub.hex())
                except Exception as e:
                    self.error = f"License server unreachable: {e}"
                    return self.status()
            body = json.dumps({"key": self.key, "machine": self.machine, "name": platform.node()[:80], "version": VERSION}).encode()
            req = urllib.request.Request(self.server + "/api/license/verify", data=body, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    self._accept(json.loads(r.read())["token"])
                self.error = ""
            except urllib.error.HTTPError as e:
                try:
                    detail = json.loads(e.read()).get("detail", str(e))
                except Exception:
                    detail = str(e)
                self.error = detail
                if e.code == 404:  # key deleted / rotated: stop at once
                    self.data = {}
            except ValueError as e:  # answer did not pass the signature / PC check
                self.error = str(e)
            except Exception as e:
                self.error = f"License server unreachable ({e}). Working offline until {time.strftime('%d %b %H:%M', time.localtime(self.data.get('exp', 0)))}." if self.data else f"License server unreachable: {e}"
            self.checked = time.time()
            if not self.ok():
                print("[license]", self.reason())
            return self.status()

    def activate(self, key: str, server: str = "") -> dict:
        """Called from the web app's setup wizard: store the key (and server) and check it right away."""
        self.key = key.strip().upper()
        if server and not self.server:
            self.server = server.rstrip("/")
        self.data = {}
        self._save_file(key=self.key)
        if not self.enabled and self.server:  # first activation turns licensed mode on
            self.enabled = True
            threading.Thread(target=self._loop, daemon=True).start()
            return self.status()
        return self.refresh()

    def _save_file(self, **kw):
        f = {}
        if self.file.exists():
            try:
                f = json.loads(self.file.read_text(encoding="utf-8"))
            except Exception:
                pass
        f.update({"server": self.server, **kw})
        self.file.write_text(json.dumps(f, indent=2), encoding="utf-8")

    def _loop(self):
        while True:
            self.refresh()
            time.sleep(600 if self.ok() else 60)  # every 10 min (plan changes are also picked up right away, see app.py)
