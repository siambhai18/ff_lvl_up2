# -*- coding: utf-8 -*-
"""
FreeFire Level Up Bot - Professional Web Dashboard & Real-Time EXP Tracker
Embedded Async Web Server (aiohttp)
"""

import asyncio
import json
import os
import time
from typing import Dict, List, Any, Optional
from aiohttp import web


# Valid match modes. AUTO = automatic switch (BR until Level 10, then Lone Wolf).
VALID_MODES = {"BR", "LONE_WOLF", "AUTO"}
MAX_HISTORY = 200


def _normalize_mode(mode: str) -> Optional[str]:
    """Normalize any user supplied mode string to BR / LONE_WOLF / AUTO."""
    m = str(mode or "").upper().replace("-", "_").replace(" ", "_").strip()
    if m in ("LW", "LONEWOLF", "LONE_WOLF"):
        return "LONE_WOLF"
    if m == "BR":
        return "BR"
    if m in ("AUTO", "AUTOMATIC", "DEFAULT", ""):
        return "AUTO"
    return None


# Global bot state shared between Main.py and Web Dashboard
class BotState:
    def __init__(self):
        self.accounts: Dict[str, Dict[str, Any]] = {}
        self.logs: List[Dict[str, Any]] = []
        self.max_logs = 200
        self.total_matches = 0
        self.total_gained_exp = 0
        self.start_time = time.time()
        self.account_workers: Dict[str, asyncio.Task] = {}
        self.refresh_callbacks: Dict[str, Any] = {}
        self.account_credentials: Dict[str, Dict[str, Any]] = {}
        # Per-account match history: uid -> list of match detail dicts
        self.account_history: Dict[str, List[Dict[str, Any]]] = {}

    def log(self, message: str, level: str = "info", uid: Optional[str] = None):
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "level": level,
            "message": message,
            "uid": uid
        }
        self.logs.append(entry)
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    def register_account(self, uid: str, nickname: str, region: str, level: int, exp: int, likes: int = 0):
        uid_str = str(uid)
        lvl_val = max(1, int(level or 1))
        if uid_str not in self.accounts:
            self.accounts[uid_str] = {
                "uid": uid_str,
                "nickname": nickname or f"Player_{uid_str[:6]}",
                "region": region or "BD",
                "level": lvl_val,
                "initial_exp": exp,
                "current_exp": exp,
                "gained_exp": 0,
                "likes": likes or 0,
                "mode": "AUTO",
                "mode_manual": False,
                "auto_match": False,
                "match_running": False,
                "match_limit": 0,
                "limit_baseline": 0,
                "status": "ONLINE",
                "matches_played": 0,
                "active_matches": 0,
                "last_match_time": None,
                "last_updated": time.strftime("%H:%M:%S")
            }
        else:
            acc = self.accounts[uid_str]
            if nickname:
                acc["nickname"] = nickname
            if region:
                acc["region"] = region
            if level:
                acc["level"] = lvl_val
            acc["current_exp"] = exp
            acc["gained_exp"] = max(0, exp - acc["initial_exp"])
            acc["likes"] = likes
            acc["status"] = "ONLINE"
            acc["last_updated"] = time.strftime("%H:%M:%S")
            # Backfill new fields for accounts created by an older version.
            acc.setdefault("mode", "AUTO")
            acc.setdefault("mode_manual", False)
            acc.setdefault("auto_match", False)
            acc.setdefault("match_running", False)
            acc.setdefault("match_limit", 0)
            acc.setdefault("limit_baseline", 0)
        self.recalc_totals()

    def update_exp(self, uid: str, current_exp: int, level: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            acc = self.accounts[uid_str]
            old_exp = acc["current_exp"]
            acc["current_exp"] = current_exp
            if level is not None and level > 0:
                acc["level"] = int(level)
            acc["gained_exp"] = max(0, current_exp - acc["initial_exp"])
            acc["last_updated"] = time.strftime("%H:%M:%S")
            diff = current_exp - old_exp
            if diff > 0:
                self.log(
                    f"Account {acc['nickname']} ({uid_str}) gained +{diff} EXP! "
                    f"Total Gained: +{acc['gained_exp']} | Mode: {acc.get('mode', 'AUTO')}",
                    "success", uid_str
                )
            self.recalc_totals()

    def get_account_level(self, uid: str) -> int:
        acc = self.accounts.get(str(uid))
        return int(acc.get("level", 1) or 1) if acc else 1

    def get_account_mode(self, uid: str) -> str:
        """Return the stored mode: BR, LONE_WOLF or AUTO."""
        acc = self.accounts.get(str(uid))
        if not acc:
            return "AUTO"
        return _normalize_mode(acc.get("mode")) or "AUTO"

    def get_effective_mode(self, uid: str) -> str:
        """Resolve the mode that should actually be played right now."""
        acc = self.accounts.get(str(uid))
        if not acc:
            return "BR"
        mode = _normalize_mode(acc.get("mode")) or "AUTO"
        level = int(acc.get("level", 1) or 1)
        if mode == "BR":
            return "BR"
        if mode == "LONE_WOLF":
            return "LONE_WOLF"
        # AUTO: Battle Royale through Level 10, then Lone Wolf.
        return "LONE_WOLF" if level > 10 else "BR"

    def set_account_mode(self, uid: str, mode: str):
        uid_str = str(uid)
        norm = _normalize_mode(mode)
        if not norm:
            return False, "Invalid mode"

        acc = self.accounts.get(uid_str)
        if not acc:
            return False, "Account not found"

        level = int(acc.get("level", 1) or 1)
        if norm == "LONE_WOLF" and level < 5:
            return False, "Lone Wolf is locked until Level 5"

        acc["mode"] = norm
        acc["mode_manual"] = norm != "AUTO"
        acc["last_updated"] = time.strftime("%H:%M:%S")
        self.log(
            f"UID {uid_str} ({acc.get('nickname', 'Unknown')}) mode set to {norm}",
            "success", uid_str
        )
        return True, norm

    # ---------- Match running / auto-match ----------
    def set_initial_running(self, uid: str, auto_match: bool, force: bool = False):
        """Apply the auto-match preference once, when an account first connects.

        Guarded by an in-memory flag so that later reconnects do not override a
        manual Start/Stop decision the user made from the dashboard.
        """
        acc = self.accounts.get(str(uid))
        if not acc:
            return
        acc["auto_match"] = bool(auto_match)
        if acc.get("_auto_init_done") and not force:
            return
        acc["match_running"] = bool(auto_match)
        acc["_auto_init_done"] = True
        acc["last_updated"] = time.strftime("%H:%M:%S")

    def is_account_running(self, uid: str) -> bool:
        acc = self.accounts.get(str(uid))
        if not acc:
            return False
        return bool(acc.get("match_running", False))

    def set_account_running(self, uid: str, running: bool):
        uid_str = str(uid)
        acc = self.accounts.get(uid_str)
        if not acc:
            return False, "Account not found"
        acc["match_running"] = bool(running)
        acc["last_updated"] = time.strftime("%H:%M:%S")
        if running:
            # Starting a fresh run: reset the limit baseline so the counter is
            # measured from this moment.
            acc["limit_baseline"] = int(acc.get("matches_played", 0) or 0)
        self.log(
            f"UID {uid_str} ({acc.get('nickname', 'Unknown')}) matches "
            f"{'STARTED' if running else 'STOPPED'}",
            "success" if running else "warning", uid_str
        )
        return True, bool(running)

    # ---------- Match limit ----------
    def set_account_limit(self, uid: str, limit: int):
        uid_str = str(uid)
        acc = self.accounts.get(uid_str)
        if not acc:
            return False, "Account not found"
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 0
        if limit < 0:
            limit = 0
        acc["match_limit"] = limit
        acc["limit_baseline"] = int(acc.get("matches_played", 0) or 0)
        acc["last_updated"] = time.strftime("%H:%M:%S")
        label = "unlimited" if limit == 0 else str(limit)
        self.log(
            f"UID {uid_str} match limit set to {label}",
            "info", uid_str
        )
        return True, limit

    def get_account_limit(self, uid: str) -> int:
        acc = self.accounts.get(str(uid))
        if not acc:
            return 0
        return int(acc.get("match_limit", 0) or 0)

    def is_limit_reached(self, uid: str) -> bool:
        acc = self.accounts.get(str(uid))
        if not acc:
            return False
        limit = int(acc.get("match_limit", 0) or 0)
        if limit <= 0:
            return False
        baseline = int(acc.get("limit_baseline", 0) or 0)
        played = int(acc.get("matches_played", 0) or 0)
        return (played - baseline) >= limit

    # ---------- Match history ----------
    def record_match(self, uid: str, match_index: int, mode: str, duration: float,
                     exp_before: int, exp_after: int, result: str = "completed"):
        uid_str = str(uid)
        entry = {
            "match_index": int(match_index),
            "mode": str(mode or "BR"),
            "duration": int(max(0, duration)),
            "exp_before": int(exp_before or 0),
            "exp_after": int(exp_after or 0),
            "exp_gained": max(0, int(exp_after or 0) - int(exp_before or 0)),
            "result": result,
            "time": time.strftime("%H:%M:%S"),
            "timestamp": time.time(),
        }
        hist = self.account_history.setdefault(uid_str, [])
        hist.append(entry)
        if len(hist) > MAX_HISTORY:
            del hist[:-MAX_HISTORY]

    def get_account_history(self, uid: str) -> List[Dict[str, Any]]:
        return list(self.account_history.get(str(uid), []))

    def update_status(self, uid: str, status: str, active_matches: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["status"] = status
            if active_matches is not None:
                self.accounts[uid_str]["active_matches"] = active_matches
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")

    def increment_match(self, uid: str):
        uid_str = str(uid)
        self.total_matches += 1
        if uid_str in self.accounts:
            self.accounts[uid_str]["matches_played"] += 1
            self.accounts[uid_str]["last_match_time"] = time.strftime("%H:%M:%S")
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")
            self.log(f"Account {self.accounts[uid_str]['nickname']} finished Match #{self.accounts[uid_str]['matches_played']}", "info", uid_str)

    def recalc_totals(self):
        self.total_gained_exp = sum(acc.get("gained_exp", 0) for acc in self.accounts.values())


bot_state = BotState()

# Convenience cursor for the dashboard batch importer.
batch_import_state = {"last_start": 0, "last_count": 0, "last_end": 0, "total": 0}


# ==================== HTTP HANDLERS ====================

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "index.html")

async def handle_index(request: web.Request) -> web.Response:
    if os.path.exists(TEMPLATE_PATH):
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        content = "<h1>templates/index.html not found!</h1>"
    return web.Response(text=content, content_type="text/html", charset="utf-8")


async def handle_get_stats(request: web.Request) -> web.Response:
    accounts_data = list(bot_state.accounts.values())
    accounts_data.sort(key=lambda x: x.get("gained_exp", 0), reverse=True)
    return web.json_response({
        "total_accounts": len(bot_state.accounts),
        "total_matches": bot_state.total_matches,
        "total_gained_exp": bot_state.total_gained_exp,
        "accounts": accounts_data,
        "logs": bot_state.logs[-60:],
        "uptime": int(time.time() - bot_state.start_time),
        "import_state": dict(batch_import_state)
    })


async def handle_add_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        accounts_file = "accounts.json"
        existing = []
        if os.path.exists(accounts_file):
            try:
                with open(accounts_file, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = []

        auto_match = bool(data.get("auto_match", False))

        if "uid" in data and "password" in data:
            uid = str(data["uid"]).strip()
            pwd = str(data["password"]).strip()
            if not uid or not pwd:
                return web.json_response({"status": "error", "error": "UID and Password are required"})
            existing = [acc for acc in existing if str(acc.get("uid")) != uid]
            existing.append({"uid": uid, "password": pwd, "auto_match": auto_match})
        elif "token" in data:
            token = str(data["token"]).strip()
            if not token:
                return web.json_response({"status": "error", "error": "Token is required"})
            existing = [acc for acc in existing if acc.get("token") != token]
            existing.append({"token": token, "auto_match": auto_match})
        else:
            return web.json_response({"status": "error", "error": "Invalid payload"})

        with open(accounts_file, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)

        bot_state.log(f"New account added: {data.get('uid') or 'Token'} (auto-match: {'on' if auto_match else 'off'})", "success")

        # Trigger dynamic worker launch
        if "on_account_added" in bot_state.refresh_callbacks:
            payload = dict(data)
            payload["auto_match"] = auto_match
            asyncio.create_task(bot_state.refresh_callbacks["on_account_added"](payload))

        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_delete_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        token = str(data.get("token", "")).strip()
        accounts_file = "accounts.json"
        if os.path.exists(accounts_file):
            with open(accounts_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if token:
                existing = [acc for acc in existing if str(acc.get("token", "")).strip() != token]
            elif uid:
                existing = [acc for acc in existing if str(acc.get("uid", "")).strip() != uid]
            with open(accounts_file, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2)

        worker_keys = []
        if uid:
            worker_keys.append(uid)
        if token:
            worker_keys.append(token[:10])
        for key in worker_keys:
            worker = bot_state.account_workers.get(key)
            if worker:
                if not worker.done():
                    worker.cancel()
                bot_state.account_workers.pop(key, None)

        if uid and uid in bot_state.accounts:
            del bot_state.accounts[uid]
        bot_state.account_history.pop(uid, None)
        bot_state.log(f"Account {uid or token[:10] or 'unknown'} removed from rotation.", "warning", uid or None)
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def _load_accounts_file() -> List[Dict[str, Any]]:
    if not os.path.exists("accounts.json"):
        return []
    try:
        with open("accounts.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_accounts_file(accounts: List[Dict[str, Any]]) -> None:
    with open("accounts.json", "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2, ensure_ascii=False)


def _normalize_uploaded_accounts(payload: Any) -> tuple[List[Dict[str, str]], Optional[str]]:
    if isinstance(payload, dict):
        for key in ("accounts", "data", "users"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        return [], "JSON must contain an account list (array or accounts/data/users array)"

    result: List[Dict[str, str]] = []
    for pos, item in enumerate(payload, 1):
        if not isinstance(item, dict):
            return [], f"Account #{pos} is not an object"
        uid = str(item.get("uid", "") or "").strip()
        password = str(item.get("password", "") or item.get("pass", "") or "").strip()
        token = str(item.get("token", "") or "").strip()
        if uid and password:
            result.append({"uid": uid, "password": password})
        elif token:
            result.append({"token": token})
        else:
            return [], f"Account #{pos} needs uid + password, or token"
    return result, None


def _parse_txt_accounts(raw_text: str) -> List[Dict[str, str]]:
    """Parse a plain-text account list.

    Every non-empty line must contain a UID and a password separated by one of
    ':'  '|'  ','  TAB  or whitespace.  Lines starting with '#' are ignored.
    """
    result: List[Dict[str, str]] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = None
        for sep in (":", "|", ",", "\t"):
            if sep in line:
                parts = line.split(sep, 1)
                break
        if parts is None:
            parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        uid = parts[0].strip()
        pwd = parts[1].strip()
        if uid and pwd:
            result.append({"uid": uid, "password": pwd})
    return result


async def handle_batch_import(request: web.Request) -> web.Response:
    global batch_import_state
    try:
        reader = await request.multipart()
        file_bytes = None
        file_filename = ""
        start_raw, count_raw = "1", "1"
        auto_match = False
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "file":
                file_filename = part.filename or ""
                file_bytes = await part.read(decode=False)
            elif part.name == "start":
                start_raw = (await part.text()).strip() or "1"
            elif part.name == "count":
                count_raw = (await part.text()).strip() or "1"
            elif part.name == "auto_match":
                auto_match = (await part.text()).strip().lower() in ("1", "true", "on", "yes")

        if file_bytes is None or not file_filename:
            return web.json_response({"status": "error", "error": "Please select a JSON or TXT file"}, status=400)
        try:
            start, count = int(start_raw), int(count_raw)
        except ValueError:
            return web.json_response({"status": "error", "error": "Start and count must be whole numbers"}, status=400)
        if start < 1 or count < 1:
            return web.json_response({"status": "error", "error": "Start must be >= 1 and count must be >= 1"}, status=400)
        if count > 500:
            return web.json_response({"status": "error", "error": "Maximum 500 accounts per batch"}, status=400)

        raw = file_bytes
        if len(raw) > 2 * 1024 * 1024:
            return web.json_response({"status": "error", "error": "File is too large (max 2 MB)"}, status=400)

        lower_name = file_filename.lower()
        is_txt = lower_name.endswith(".txt")

        source: List[Dict[str, str]] = []
        err: Optional[str] = None
        if is_txt:
            try:
                text = raw.decode("utf-8-sig")
            except Exception:
                text = raw.decode("utf-8", errors="ignore")
            source = _parse_txt_accounts(text)
            if not source:
                return web.json_response({"status": "error", "error": "TXT contains no valid 'uid:password' lines"}, status=400)
        else:
            try:
                payload = json.loads(raw.decode("utf-8-sig"))
            except Exception:
                # Fall back to TXT parsing so a plain text file still works.
                try:
                    text = raw.decode("utf-8-sig")
                except Exception:
                    text = raw.decode("utf-8", errors="ignore")
                source = _parse_txt_accounts(text)
                if not source:
                    return web.json_response({"status": "error", "error": "Invalid JSON or TXT file"}, status=400)
            else:
                source, err = _normalize_uploaded_accounts(payload)
                if err:
                    return web.json_response({"status": "error", "error": err}, status=400)

        total = len(source)
        if total == 0:
            return web.json_response({"status": "error", "error": "File contains no accounts"}, status=400)
        if start > total:
            return web.json_response({"status": "error", "error": f"Start account #{start} is outside the file (total {total})"}, status=400)

        end = min(total, start + count - 1)
        selected = source[start - 1:end]
        existing = await _load_accounts_file()
        for acc in selected:
            acc = dict(acc)
            acc["auto_match"] = auto_match
            if "uid" in acc:
                existing = [x for x in existing if str(x.get("uid", "")).strip() != acc["uid"]]
            else:
                existing = [x for x in existing if str(x.get("token", "")).strip() != acc["token"]]
            existing.append(acc)
        _save_accounts_file(existing)

        batch_import_state = {"last_start": start, "last_count": len(selected), "last_end": end, "total": total}
        callback = bot_state.refresh_callbacks.get("on_account_added")
        started = 0
        if callback:
            for acc in selected:
                payload = dict(acc)
                payload["auto_match"] = auto_match
                await callback(payload)
                started += 1
                if started < len(selected):
                    await asyncio.sleep(0.20)

        bot_state.log(
            f"Batch import: accounts {start}-{end} selected from {total}; {len(selected)} loaded "
            f"(auto-match: {'on' if auto_match else 'off'}).",
            "success"
        )
        return web.json_response({"status": "ok", "total": total, "start": start, "count": len(selected), "end": end, "next_start": end + 1 if end < total else None, "started": started, "message": f"Accounts {start}-{end} imported"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=500)


async def handle_delete_all_accounts(request: web.Request) -> web.Response:
    try:
        accounts = await _load_accounts_file()
        removed = len(accounts)
        workers = list(bot_state.account_workers.values())
        for worker in workers:
            if worker and not worker.done():
                worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        bot_state.account_workers.clear()
        _save_accounts_file([])
        bot_state.accounts.clear()
        bot_state.account_credentials.clear()
        bot_state.account_history.clear()
        bot_state.recalc_totals()
        bot_state.log(f"All accounts deleted. {removed} account(s) removed from rotation.", "warning")
        return web.json_response({"status": "ok", "removed": removed})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=500)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "accounts": len(bot_state.accounts), "uptime": int(time.time() - bot_state.start_time)})


async def handle_refresh_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid")).strip()
        if "on_refresh_account" in bot_state.refresh_callbacks:
            asyncio.create_task(bot_state.refresh_callbacks["on_refresh_account"](uid))
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_restart_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if "on_restart_account" in bot_state.refresh_callbacks:
            asyncio.create_task(bot_state.refresh_callbacks["on_restart_account"](uid))
            return web.json_response({"status": "ok"})
        return web.json_response({"status": "error", "error": "Restart is not available"}, status=400)
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_mode_switch(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        mode = str(data.get("mode", "")).strip()
        ok, result = bot_state.set_account_mode(uid, mode)
        if not ok:
            return web.json_response({"status": "error", "error": result}, status=400)
        return web.json_response({
            "status": "ok",
            "uid": uid,
            "mode": result,
            "effective_mode": bot_state.get_effective_mode(uid),
            "level": bot_state.get_account_level(uid)
        })
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_account_start(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        ok, result = bot_state.set_account_running(uid, True)
        if not ok:
            return web.json_response({"status": "error", "error": result}, status=400)
        return web.json_response({"status": "ok", "uid": uid, "running": True})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_account_stop(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        ok, result = bot_state.set_account_running(uid, False)
        if not ok:
            return web.json_response({"status": "error", "error": result}, status=400)
        return web.json_response({"status": "ok", "uid": uid, "running": False})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_start_all(request: web.Request) -> web.Response:
    try:
        started = 0
        for uid in list(bot_state.accounts.keys()):
            ok, _ = bot_state.set_account_running(uid, True)
            if ok:
                started += 1
        bot_state.log(f"All nodes started ({started}).", "success")
        return web.json_response({"status": "ok", "started": started})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=500)


async def handle_stop_all(request: web.Request) -> web.Response:
    try:
        stopped = 0
        for uid in list(bot_state.accounts.keys()):
            ok, _ = bot_state.set_account_running(uid, False)
            if ok:
                stopped += 1
        bot_state.log(f"All nodes stopped ({stopped}).", "warning")
        return web.json_response({"status": "ok", "stopped": stopped})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=500)


async def handle_account_settings(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        acc = bot_state.accounts.get(uid)
        if not acc:
            return web.json_response({"status": "error", "error": "Account not found"}, status=400)

        result = {"status": "ok", "uid": uid}

        if "match_limit" in data:
            ok, limit = bot_state.set_account_limit(uid, data.get("match_limit", 0))
            if not ok:
                return web.json_response({"status": "error", "error": limit}, status=400)
            result["match_limit"] = limit

        if "mode" in data and str(data.get("mode", "")).strip():
            ok, mode = bot_state.set_account_mode(uid, data.get("mode"))
            if not ok:
                return web.json_response({"status": "error", "error": mode}, status=400)
            result["mode"] = mode

        result["effective_mode"] = bot_state.get_effective_mode(uid)
        return web.json_response(result)
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_account_history(request: web.Request) -> web.Response:
    try:
        uid = str(request.query.get("uid", "")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "uid is required"}, status=400)
        hist = bot_state.get_account_history(uid)
        total_exp = sum(h.get("exp_gained", 0) for h in hist)
        total_time = sum(h.get("duration", 0) for h in hist)
        acc = bot_state.accounts.get(uid, {})
        return web.json_response({
            "status": "ok",
            "uid": uid,
            "nickname": acc.get("nickname", ""),
            "mode": acc.get("mode", "AUTO"),
            "match_limit": acc.get("match_limit", 0),
            "matches_played": acc.get("matches_played", 0),
            "history": hist,
            "total_matches": len(hist),
            "total_exp": total_exp,
            "total_time": total_time,
        })
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def start_web_dashboard(host: str = "0.0.0.0", port: int = 5000):
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/stats", handle_get_stats)
    app.router.add_post("/api/account/add", handle_add_account)
    app.router.add_post("/api/account/delete", handle_delete_account)
    app.router.add_post("/api/account/import", handle_batch_import)
    app.router.add_post("/api/account/delete-all", handle_delete_all_accounts)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/api/account/refresh", handle_refresh_account)
    app.router.add_post("/api/account/restart", handle_restart_account)
    app.router.add_post("/api/account/mode", handle_mode_switch)
    app.router.add_post("/api/account/start", handle_account_start)
    app.router.add_post("/api/account/stop", handle_account_stop)
    app.router.add_post("/api/account/start-all", handle_start_all)
    app.router.add_post("/api/account/stop-all", handle_stop_all)
    app.router.add_post("/api/account/settings", handle_account_settings)
    app.router.add_get("/api/account/history", handle_account_history)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"\033[92m[+] Web Dashboard running on http://localhost:{port}\033[0m")
