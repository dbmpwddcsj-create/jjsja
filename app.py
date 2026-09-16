import asyncio
import logging
import os
import re
import sqlite3
import threading
import time
import traceback
import uuid as _uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Optional

from flask import (
    Flask,
    jsonify,
    render_template_string,
    request,
    session,
)
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    InputUserDeactivatedError,
    PasswordHashInvalidError,
    PeerFloodError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
    UserNotMutualContactError,
    UserPrivacyRestrictedError,
)
from telethon.tl.types import User

import requests as _requests
from bs4 import BeautifulSoup


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

def _pick_data_dir():
    preferred = os.getenv("DATA_DIR", "").strip()
    candidates = []

    if preferred:
        candidates.append(preferred)

    candidates.append("/var/data")
    candidates.append(str(Path(__file__).resolve().parent / "data"))
    candidates.append("/tmp/data")

    for path in candidates:
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".writable_test")
            with open(probe, "w") as handle:
                handle.write("ok")
            os.remove(probe)
            return path
        except Exception:
            continue

    raise RuntimeError("No writable directory found.")


DATA_DIR = _pick_data_dir()
DB_PATH = Path(os.getenv("DB_PATH", os.path.join(DATA_DIR, "sender.sqlite3")))
SESSION_DIR = Path(os.getenv("TG_SESSION_DIR", os.path.join(DATA_DIR, "sessions")))
SESSION_DIR.mkdir(parents=True, exist_ok=True)
SESSION_PATH = SESSION_DIR / "main"

TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
FLASK_SECRET = os.getenv("FLASK_SECRET", "") or os.urandom(32).hex()

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5000"))


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger("telegram-sender")


# -----------------------------------------------------------------------------
# Flask
# -----------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = FLASK_SECRET
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024


# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------

class Database:
    def __init__(self, path: Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._local = threading.local()
        self.init_db()

    def connection(self):
        conn = getattr(self._local, "connection", None)

        if conn is None:
            conn = sqlite3.connect(
                self.path,
                timeout=30,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.connection = conn

        return conn

    def close(self):
        conn = getattr(self._local, "connection", None)

        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.connection = None

    def execute(self, sql, params=()):
        with self._lock:
            conn = self.connection()
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor

    def executemany(self, sql, rows):
        with self._lock:
            conn = self.connection()
            cursor = conn.executemany(sql, rows)
            conn.commit()
            return cursor

    def fetchone(self, sql, params=()):
        with self._lock:
            return self.connection().execute(sql, params).fetchone()

    def fetchall(self, sql, params=()):
        with self._lock:
            return self.connection().execute(sql, params).fetchall()

    def init_db(self):
        self.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )

        self.execute(
            """
            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL UNIQUE
            )
            """
        )

        self.execute(
            """
            CREATE TABLE IF NOT EXISTS excluded_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL UNIQUE
            )
            """
        )

        self.execute(
            """
            CREATE TABLE IF NOT EXISTS contacted (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                group_title TEXT,
                sent_at TEXT NOT NULL,
                status TEXT NOT NULL
            )
            """
        )

    # --- settings ----------------------------------------------------------

    def get_setting(self, key, default=None):
        row = self.fetchone(
            "SELECT value FROM settings WHERE key = ?",
            (key,),
        )
        if row is None:
            return default
        return row["value"]

    def set_setting(self, key, value):
        self.execute(
            """
            INSERT INTO settings(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )

    # --- keywords ----------------------------------------------------------

    def get_keywords(self):
        rows = self.fetchall("SELECT keyword FROM keywords ORDER BY id")
        return [row["keyword"] for row in rows]

    def add_keyword(self, keyword):
        keyword = str(keyword or "").strip()

        if not keyword:
            raise ValueError("Empty keyword.")

        if len(keyword) > 200:
            raise ValueError("Keyword is too long.")

        self.execute(
            "INSERT OR IGNORE INTO keywords(keyword) VALUES (?)",
            (keyword,),
        )

    def delete_keyword(self, keyword):
        self.execute(
            "DELETE FROM keywords WHERE keyword = ?",
            (keyword,),
        )

    # --- excluded groups ---------------------------------------------------

    def get_excluded_titles(self):
        rows = self.fetchall(
            "SELECT title FROM excluded_groups ORDER BY id"
        )
        return [row["title"] for row in rows]

    def add_excluded_title(self, title):
        title = str(title or "").strip()

        if not title:
            raise ValueError("Empty group title.")

        if len(title) > 300:
            raise ValueError("Group title is too long.")

        self.execute(
            "INSERT OR IGNORE INTO excluded_groups(title) VALUES (?)",
            (title,),
        )

    def delete_excluded_title(self, title):
        self.execute(
            "DELETE FROM excluded_groups WHERE title = ?",
            (title,),
        )

    def is_excluded_title(self, title):
        row = self.fetchone(
            "SELECT 1 FROM excluded_groups WHERE title = ?",
            (str(title),),
        )
        return row is not None

    # --- contacted ---------------------------------------------------------

    def get_contacted_ids(self):
        rows = self.fetchall("SELECT user_id FROM contacted")
        return {row["user_id"] for row in rows}

    def add_contact(
        self,
        user_id,
        username,
        first_name,
        group_title,
        status,
    ):
        self.execute(
            """
            INSERT OR IGNORE INTO contacted(
                user_id, username, first_name,
                group_title, sent_at, status
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                username or "",
                first_name or "",
                group_title or "",
                datetime.now(timezone.utc).isoformat(),
                status,
            ),
        )

    def clear_contacts(self):
        self.execute("DELETE FROM contacted")

    def count_contacts(self):
        row = self.fetchone(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent,
                SUM(CASE WHEN status = 'skipped_had_history' THEN 1 ELSE 0 END) AS skipped,
                SUM(CASE WHEN status = 'privacy_blocked' THEN 1 ELSE 0 END) AS blocked
            FROM contacted
            """
        )

        return {
            "total": row["total"] or 0,
            "sent": row["sent"] or 0,
            "skipped": row["skipped"] or 0,
            "blocked": row["blocked"] or 0,
        }


db = Database(DB_PATH)


@app.teardown_appcontext
def close_database(_error=None):
    db.close()


# -----------------------------------------------------------------------------
# Async Telethon runtime
# -----------------------------------------------------------------------------

class TelegramRuntime:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self._run_loop,
            name="telegram-event-loop",
            daemon=True,
        )

        self.client: Optional[TelegramClient] = None
        self.client_lock = threading.RLock()

        self.state_lock = threading.RLock()
        self.state = {
            "authorized": False,
            "phone": None,
            "auth_pending": False,
            "auth_error": None,
            "scan_running": False,
            "scan_stop_requested": False,
            "scan_started_at": None,
            "scan_finished_at": None,
            "scan_status": "idle",
            "scan_error": None,
            "progress": "",
            "sent_count": 0,
            "skipped_count": 0,
            "error_count": 0,
            "last_log": [],
        }

        self.phone_code_hash = None

        self.thread.start()

    # --- loop plumbing -----------------------------------------------------

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    def snapshot(self):
        with self.state_lock:
            return dict(self.state)

    def update_state(self, **values):
        with self.state_lock:
            self.state.update(values)

    def log(self, message):
        with self.state_lock:
            self.state["last_log"].append(
                datetime.now().strftime("%H:%M:%S") + " " + str(message)
            )
            self.state["last_log"] = self.state["last_log"][-200:]

    def is_stop_requested(self):
        with self.state_lock:
            return bool(self.state["scan_stop_requested"])

    def request_stop(self):
        with self.state_lock:
            self.state["scan_stop_requested"] = True

    # --- client ------------------------------------------------------------

    async def _create_client(self):
        # ВАЖНО: создаём клиент внутри работающего event loop,
        # иначе Python 3.14 падает с "no current event loop".
        return TelegramClient(
            str(SESSION_PATH),
            TG_API_ID,
            TG_API_HASH,
        )

    def get_client(self):
        with self.client_lock:
            if self.client is None:
                self.client = self.submit(self._create_client()).result(
                    timeout=30
                )
            return self.client

    async def ensure_connected(self):
        client = self.get_client()

        if not client.is_connected():
            await client.connect()

        authorized = await client.is_user_authorized()
        self.update_state(authorized=authorized)

        return client, authorized

    async def disconnect(self):
        with self.client_lock:
            client = self.client

        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                logger.exception("Error disconnecting Telegram client.")

        self.update_state(authorized=False)

    # --- auth --------------------------------------------------------------

    async def send_code(self, phone):
        client = self.get_client()

        if not client.is_connected():
            await client.connect()

        result = await client.send_code_request(phone)
        self.phone_code_hash = result.phone_code_hash

        self.update_state(
            phone=phone,
            auth_pending=True,
            auth_error=None,
        )

    async def sign_in_code(self, phone, code):
        client = self.get_client()

        if not client.is_connected():
            await client.connect()

        try:
            await client.sign_in(
                phone=phone,
                code=code,
                phone_code_hash=self.phone_code_hash,
            )

            authorized = await client.is_user_authorized()

            self.update_state(
                authorized=authorized,
                phone=phone,
                auth_pending=False,
                auth_error=None,
            )

        except SessionPasswordNeededError:
            self.update_state(
                phone=phone,
                auth_pending=True,
                auth_error="2FA password required.",
            )
            raise

    async def sign_in_password(self, password):
        client = self.get_client()

        if not client.is_connected():
            await client.connect()

        await client.sign_in(password=password)

        authorized = await client.is_user_authorized()

        self.update_state(
            authorized=authorized,
            auth_pending=False,
            auth_error=None,
        )

    # --- scan + mass send --------------------------------------------------

    async def scan_and_send(self):
        client, authorized = await self.ensure_connected()

        if not authorized:
            raise RuntimeError("Telegram account is not authorized.")

        keywords = db.get_keywords()

        if not keywords:
            raise RuntimeError("Add at least one keyword before scanning.")

        normalized_keywords = [k.casefold() for k in keywords]

        excluded_titles = {
            t.casefold() for t in db.get_excluded_titles()
        }

        message_text = db.get_setting("message", "Привет!")
        min_delay = float(db.get_setting("min_delay", "3"))
        max_per_hour = int(db.get_setting("max_per_hour", "20"))

        contacted_ids = db.get_contacted_ids()

        self.update_state(
            scan_running=True,
            scan_stop_requested=False,
            scan_started_at=datetime.now(timezone.utc).isoformat(),
            scan_finished_at=None,
            scan_status="running",
            scan_error=None,
            progress="Получаю список групп...",
            sent_count=0,
            skipped_count=0,
            error_count=0,
            last_log=[],
        )

        hour_window_start = time.time()
        sent_in_window = 0

        try:
            dialogs = await client.get_dialogs()
            groups = [d for d in dialogs if d.is_group]
            total_groups = len(groups)

            self.log("Найдено групп: " + str(total_groups))

            for g_idx, dialog in enumerate(groups, 1):
                if self.is_stop_requested():
                    self.log("Остановка по запросу.")
                    break

                title = dialog.title or ""

                if title.casefold() in excluded_titles:
                    self.log(
                        "[" + str(g_idx) + "/" + str(total_groups)
                        + "] Пропуск исключённой: " + title
                    )
                    continue

                self.update_state(
                    progress="Скан группы " + str(g_idx)
                    + "/" + str(total_groups) + ": " + title
                )
                self.log(
                    "[" + str(g_idx) + "/" + str(total_groups)
                    + "] Сканирую: " + title
                )

                try:
                    async for message in client.iter_messages(dialog.id):
                        if self.is_stop_requested():
                            break

                        if not message.sender_id:
                            continue

                        text = message.message or ""

                        if not text:
                            continue

                        lowered = text.casefold()
                        matched = False

                        for keyword in normalized_keywords:
                            if keyword in lowered:
                                matched = True
                                break

                        if not matched:
                            continue

                        try:
                            sender = await message.get_sender()
                        except Exception:
                            continue

                        if not isinstance(sender, User):
                            continue

                        if getattr(sender, "bot", False):
                            continue

                        if getattr(sender, "is_self", False):
                            continue

                        uid = getattr(sender, "id", None)

                        if not uid:
                            continue

                        if uid in contacted_ids:
                            with self.state_lock:
                                self.state["skipped_count"] += 1
                            continue

                        # лимит в час
                        now = time.time()

                        if now - hour_window_start >= 3600:
                            hour_window_start = now
                            sent_in_window = 0

                        if sent_in_window >= max_per_hour:
                            wait = 3600 - (now - hour_window_start)

                            if wait > 0:
                                self.log(
                                    "Лимит " + str(max_per_hour)
                                    + "/час. Пауза "
                                    + str(int(wait)) + " сек..."
                                )
                                await asyncio.sleep(wait)
                                hour_window_start = time.time()
                                sent_in_window = 0

                        # проверяем, что в личке вообще пусто
                        try:
                            history = await client.get_messages(
                                sender, limit=1
                            )
                            has_history = bool(history)
                        except Exception as exc:
                            self.log(
                                "  Ошибка проверки истории "
                                + str(uid) + ": " + str(exc)
                            )
                            continue

                        if has_history:
                            db.add_contact(
                                uid,
                                sender.username or "",
                                sender.first_name or "",
                                title,
                                "skipped_had_history",
                            )
                            contacted_ids.add(uid)

                            with self.state_lock:
                                self.state["skipped_count"] += 1

                            continue

                        # отправка
                        try:
                            await client.send_message(sender, message_text)

                            sent_in_window += 1

                            with self.state_lock:
                                self.state["sent_count"] += 1

                            self.log(
                                "  OK: "
                                + str(sender.first_name or "")
                                + " (@" + str(sender.username or uid) + ")"
                            )

                            db.add_contact(
                                uid,
                                sender.username or "",
                                sender.first_name or "",
                                title,
                                "sent",
                            )
                            contacted_ids.add(uid)

                        except (
                            UserPrivacyRestrictedError,
                            UserNotMutualContactError,
                        ) as exc:
                            self.log(
                                "  Приватность: " + str(uid)
                                + " (" + type(exc).__name__ + ")"
                            )

                            with self.state_lock:
                                self.state["error_count"] += 1

                            db.add_contact(
                                uid,
                                sender.username or "",
                                sender.first_name or "",
                                title,
                                "privacy_blocked",
                            )
                            contacted_ids.add(uid)

                        except PeerFloodError:
                            self.log(
                                "  PeerFloodError — Telegram ограничил "
                                "отправку. Останавливаюсь."
                            )
                            return

                        except InputUserDeactivatedError:
                            self.log("  Аккаунт " + str(uid) + " удалён.")

                            with self.state_lock:
                                self.state["error_count"] += 1

                        except Exception as exc:
                            self.log(
                                "  Ошибка отправки " + str(uid)
                                + ": " + type(exc).__name__
                                + ": " + str(exc)
                            )

                            with self.state_lock:
                                self.state["error_count"] += 1

                        await asyncio.sleep(min_delay)

                except FloodWaitError:
                    raise

                except Exception as exc:
                    self.log(
                        "  Ошибка сканирования " + title
                        + ": " + type(exc).__name__ + ": " + str(exc)
                    )
                    continue

            status = (
                "stopped" if self.is_stop_requested() else "completed"
            )
            self.update_state(scan_status=status, scan_error=None)

        except FloodWaitError as exc:
            self.update_state(
                scan_status="flood_wait",
                scan_error=(
                    "Telegram requested a wait of "
                    + str(exc.seconds) + " seconds."
                ),
            )
            raise

        except Exception as exc:
            logger.exception("Scan failed.")
            self.update_state(
                scan_status="error",
                scan_error=str(exc),
            )
            raise

        finally:
            self.update_state(
                scan_running=False,
                scan_stop_requested=False,
                scan_finished_at=datetime.now(timezone.utc).isoformat(),
            )


runtime = TelegramRuntime()


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def authorized_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        state = runtime.snapshot()

        if not state["authorized"]:
            return jsonify({
                "ok": False,
                "error": "Telegram account is not authorized.",
            }), 401

        return view(*args, **kwargs)

    return wrapped


def wait_future(future, timeout=120):
    return future.result(timeout=timeout)


def validate_phone(phone):
    phone = str(phone or "").strip()

    if not re.fullmatch(r"\+?[0-9][0-9 ()-]{5,24}", phone):
        raise ValueError("Invalid phone number.")

    return phone


def validate_code(code):
    code = str(code or "").strip()

    if not re.fullmatch(r"[0-9]{3,8}", code):
        raise ValueError("Invalid verification code.")

    return code


def json_error(message, status=400):
    return jsonify({"ok": False, "error": message}), status


# -----------------------------------------------------------------------------
# /getapi in-memory sessions
# -----------------------------------------------------------------------------

_tg_sessions = {}
_tg_sessions_lock = threading.RLock()

MYTG = "https://my.telegram.org"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _new_tg_session():
    s = _requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    return s


def _get_sid():
    if "tg_sid" not in session:
        session["tg_sid"] = str(_uuid.uuid4())
    return session["tg_sid"]


# -----------------------------------------------------------------------------
# HTML — main page
# -----------------------------------------------------------------------------

PAGE = r"""
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<title>Telegram Sender</title>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, sans-serif; background:#0d1117; color:#e6edf3; margin:0; padding:24px 16px; }
  .wrap { max-width: 900px; margin: 0 auto; }
  h1 { font-size:22px; margin:0 0 20px; }
  .card { background:#161b22; border:1px solid #30363d; border-radius:12px; padding:20px; margin-bottom:16px; }
  .card h2 { font-size:15px; margin:0 0 14px; color:#58a6ff; text-transform:uppercase; letter-spacing:1px; }
  label { display:block; font-size:12px; color:#8b949e; margin:10px 0 5px; }
  input, textarea, button { width:100%; padding:9px 12px; border-radius:8px; border:1px solid #30363d; background:#0d1117; color:#e6edf3; font-size:14px; font-family:inherit; }
  textarea { min-height:100px; resize:vertical; }
  button { background:#238636; border-color:#2ea043; cursor:pointer; margin-top:12px; font-weight:600; }
  button:hover { background:#2ea043; }
  button:disabled { opacity:0.6; cursor:wait; }
  button.secondary { background:#21262d; border-color:#30363d; }
  button.danger { background:#8b1a1a; border-color:#b62324; }
  button.small { width:auto; padding:6px 10px; font-size:12px; margin:0 0 0 8px; }
  .hidden { display:none !important; }
  .msg { margin-top:12px; font-size:13px; padding:10px; border-radius:8px; }
  .msg.err { background:#3d1618; color:#ff7b72; }
  .msg.ok { background:#0f2e1a; color:#7ee787; }
  .msg.info { background:#1c2d3d; color:#79c0ff; }
  .step-title { font-size:12px; color:#8b949e; margin-bottom:4px; text-transform:uppercase; letter-spacing:1px; }
  .row { display:flex; gap:8px; align-items:center; }
  .row input { flex:1; }
  .tag { display:inline-flex; align-items:center; background:#21262d; border:1px solid #30363d; border-radius:20px; padding:4px 10px; margin:4px 4px 0 0; font-size:13px; }
  .tag button { background:transparent; border:none; color:#ff7b72; margin:0 0 0 6px; padding:0; width:auto; cursor:pointer; font-size:14px; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  .progress { background:#0d1117; border:1px solid #30363d; border-radius:8px; padding:12px; font-family:monospace; font-size:12px; max-height:260px; overflow-y:auto; white-space:pre-wrap; }
  .stats { display:flex; gap:20px; margin-bottom:10px; }
  .stat { font-size:13px; }
  .stat b { display:block; font-size:20px; color:#7ee787; }
  .stat.skipped b { color:#d29922; }
  .stat.err b { color:#ff7b72; }
  a.getapi { color:#58a6ff; font-size:13px; display:inline-block; margin-bottom:14px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Telegram Sender</h1>
  <a class="getapi" href="/getapi">→ Получить api_id / api_hash</a>
  <div id="msg" class="msg hidden"></div>

  <div id="step-phone" class="card">
    <div class="step-title">Шаг 1 — телефон</div>
    <label>Номер (в формате +7XXXXXXXXXX)</label>
    <input id="phone" placeholder="+79991234567"/>
    <button id="btn-phone" onclick="sendCode()">Отправить код</button>
  </div>

  <div id="step-code" class="card hidden">
    <div class="step-title">Шаг 2 — код из Telegram</div>
    <label>Код</label>
    <input id="code" placeholder="12345"/>
    <button id="btn-code" onclick="verifyCode()">Подтвердить</button>
  </div>

  <div id="step-password" class="card hidden">
    <div class="step-title">Шаг 3 — пароль 2FA</div>
    <label>Облачный пароль</label>
    <input id="password" type="password"/>
    <button id="btn-pass" onclick="verifyPassword()">Войти</button>
  </div>

  <div id="panel" class="hidden">
    <div class="card">
      <h2>Ключевые слова</h2>
      <div class="row">
        <input id="kw-input" placeholder="например: купить"/>
        <button class="small" onclick="addKw()">+</button>
      </div>
      <div id="kw-list" style="margin-top:10px"></div>
    </div>

    <div class="card">
      <h2>Исключённые группы</h2>
      <div class="row">
        <input id="ex-input" placeholder="точное название группы"/>
        <button class="small" onclick="addEx()">+</button>
      </div>
      <div id="ex-list" style="margin-top:10px"></div>
    </div>

    <div class="card">
      <h2>Сообщение и лимиты</h2>
      <label>Текст сообщения</label>
      <textarea id="message"></textarea>
      <div class="grid2">
        <div>
          <label>Пауза между отправками (сек)</label>
          <input id="min_delay" type="number" min="1" value="3"/>
        </div>
        <div>
          <label>Максимум в час</label>
          <input id="max_per_hour" type="number" min="1" value="20"/>
        </div>
      </div>
      <button onclick="saveSettings()">Сохранить</button>
    </div>

    <div class="card">
      <h2>Управление</h2>
      <button id="btn-start" onclick="startScan()">▶ Начать сканирование и рассылку</button>
      <button id="btn-stop" class="danger hidden" onclick="stopScan()">■ Стоп</button>
      <div style="margin-top:14px" class="stats">
        <div class="stat">Отправлено<b id="c-sent">0</b></div>
        <div class="stat skipped">Пропущено<b id="c-skip">0</b></div>
        <div class="stat err">Ошибок<b id="c-err">0</b></div>
      </div>
      <div id="progress" class="progress" style="margin-top:10px">—</div>
    </div>

    <div class="card">
      <button class="secondary" onclick="logout()">Выйти из аккаунта</button>
    </div>
  </div>
</div>

<script>
console.log("[init] script loaded");
const $ = function(id){ return document.getElementById(id); };
const msg = $("msg");
let pollTimer = null;

function show(t, kind) {
  if (kind === undefined) kind = "err";
  if (t === undefined || t === null || t === "") t = "Неизвестная ошибка";
  msg.textContent = String(t);
  msg.className = "msg " + kind;
  msg.classList.remove("hidden");
  setTimeout(function(){ msg.classList.add("hidden"); }, 6000);
}

async function api(url, body, method) {
  if (method === undefined) method = "POST";
  const opts = { method: method, headers: {"Content-Type": "application/json"} };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(url, opts);
  const text = await r.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch (e) {
    throw new Error("Сервер вернул не-JSON (" + r.status + "): " + text.slice(0, 200));
  }
  return data;
}

function showAuth(id) {
  ["step-phone","step-code","step-password"].forEach(function(s){ $(s).classList.add("hidden"); });
  if (id) $(id).classList.remove("hidden");
}

async function sendCode() {
  try {
    const btn = $("btn-phone");
    const phone = $("phone").value.trim();
    if (!phone) { show("Введите номер"); return; }
    btn.disabled = true;
    show("Отправляю код...", "info");
    const res = await api("/api/send_code", { phone: phone });
    if (!res.ok) { show(res.error || "Ошибка отправки"); return; }
    if (res.already_authorized) { openPanel(); return; }
    showAuth("step-code");
    show("Код отправлен в Telegram", "ok");
  } catch (e) {
    show("Ошибка: " + e.message);
  } finally {
    $("btn-phone").disabled = false;
  }
}

async function verifyCode() {
  try {
    const btn = $("btn-code");
    const code = $("code").value.trim();
    if (!code) { show("Введите код"); return; }
    btn.disabled = true;
    show("Проверяю код...", "info");
    const res = await api("/api/verify_code", { code: code });
    if (!res.ok) { show(res.error || "Ошибка проверки"); return; }
    if (res.need_password) { showAuth("step-password"); show("Введите пароль 2FA", "info"); return; }
    openPanel();
  } catch (e) {
    show("Ошибка: " + e.message);
  } finally {
    $("btn-code").disabled = false;
  }
}

async function verifyPassword() {
  try {
    const btn = $("btn-pass");
    const password = $("password").value;
    if (!password) { show("Введите пароль"); return; }
    btn.disabled = true;
    show("Проверяю пароль...", "info");
    const res = await api("/api/verify_password", { password: password });
    if (!res.ok) { show(res.error || "Ошибка пароля"); return; }
    openPanel();
  } catch (e) {
    show("Ошибка: " + e.message);
  } finally {
    $("btn-pass").disabled = false;
  }
}

async function logout() {
  try {
    await api("/api/logout");
    location.reload();
  } catch (e) {
    show("Ошибка: " + e.message);
  }
}

async function openPanel() {
  showAuth(null);
  $("panel").classList.remove("hidden");
  try {
    await loadSettings();
    await loadKw();
    await loadEx();
  } catch (e) {
    show("Ошибка панели: " + e.message);
  }
  startPolling();
}

async function loadSettings() {
  const s = await api("/api/settings", null, "GET");
  $("message").value = s.message || "";
  $("min_delay").value = s.min_delay;
  $("max_per_hour").value = s.max_per_hour;
}

async function saveSettings() {
  try {
    await api("/api/settings", {
      message: $("message").value,
      min_delay: parseFloat($("min_delay").value || "3"),
      max_per_hour: parseInt($("max_per_hour").value || "20")
    });
    show("Настройки сохранены", "ok");
  } catch (e) { show("Ошибка: " + e.message); }
}

async function loadKw() {
  const r = await api("/api/keywords", null, "GET");
  const box = $("kw-list");
  box.innerHTML = "";
  (r.words || []).forEach(function(w){
    const el = document.createElement("span");
    el.className = "tag";
    const btn = document.createElement("button");
    btn.textContent = "×";
    btn.onclick = function(){ delKw(w); };
    el.textContent = w;
    el.appendChild(btn);
    box.appendChild(el);
  });
}

async function addKw() {
  try {
    const w = $("kw-input").value.trim();
    if (!w) return;
    await api("/api/keywords", { word: w });
    $("kw-input").value = "";
    loadKw();
  } catch (e) { show("Ошибка: " + e.message); }
}

async function delKw(word) {
  try {
    await fetch("/api/keywords/by-word/" + encodeURIComponent(word), {method:"DELETE"});
    loadKw();
  } catch (e) { show("Ошибка: " + e.message); }
}

async function loadEx() {
  const r = await api("/api/excluded", null, "GET");
  const box = $("ex-list");
  box.innerHTML = "";
  (r.groups || []).forEach(function(g){
    const el = document.createElement("span");
    el.className = "tag";
    const btn = document.createElement("button");
    btn.textContent = "×";
    btn.onclick = function(){ delEx(g); };
    el.textContent = g;
    el.appendChild(btn);
    box.appendChild(el);
  });
}

async function addEx() {
  try {
    const t = $("ex-input").value.trim();
    if (!t) return;
    await api("/api/excluded", { title: t });
    $("ex-input").value = "";
    loadEx();
  } catch (e) { show("Ошибка: " + e.message); }
}

async function delEx(title) {
  try {
    await fetch("/api/excluded/by-title/" + encodeURIComponent(title), {method:"DELETE"});
    loadEx();
  } catch (e) { show("Ошибка: " + e.message); }
}

async function startScan() {
  try {
    const res = await api("/api/scan/start");
    if (!res.ok) return show(res.error || "Не удалось запустить");
    $("btn-start").classList.add("hidden");
    $("btn-stop").classList.remove("hidden");
    startPolling();
  } catch (e) { show("Ошибка: " + e.message); }
}

async function stopScan() {
  try { await api("/api/stop"); } catch (e) { show("Ошибка: " + e.message); }
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(pollProgress, 1500);
  pollProgress();
}

async function pollProgress() {
  try {
    const p = await api("/api/progress", null, "GET");
    $("c-sent").textContent = p.sent;
    $("c-skip").textContent = p.skipped;
    $("c-err").textContent = p.errors;
    $("progress").textContent = (p.progress ? p.progress + "\n\n" : "") + (p.log || []).join("\n");
    if (p.running) {
      $("btn-start").classList.add("hidden");
      $("btn-stop").classList.remove("hidden");
    } else {
      $("btn-start").classList.remove("hidden");
      $("btn-stop").classList.add("hidden");
    }
  } catch (e) { console.log("[poll]", e); }
}

(async function(){
  try {
    const s = await api("/api/status", null, "GET");
    if (s.authorized) openPanel();
  } catch (e) { console.log("[status]", e); }
})();
</script>
</body>
</html>
"""


# -----------------------------------------------------------------------------
# HTML — /getapi
# -----------------------------------------------------------------------------

GETAPI_PAGE = r"""
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<title>Получить API_ID / API_HASH</title>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
  body { font-family: system-ui, sans-serif; background:#0d1117; color:#e6edf3; margin:0; padding:24px 16px; }
  .card { max-width:520px; margin:0 auto; background:#161b22; border:1px solid #30363d; border-radius:12px; padding:24px; }
  h1 { font-size:20px; margin:0 0 8px; }
  p.sub { color:#8b949e; font-size:13px; margin:0 0 18px; }
  label { display:block; font-size:12px; color:#8b949e; margin:10px 0 5px; }
  input, button { width:100%; padding:10px 12px; border-radius:8px; border:1px solid #30363d; background:#0d1117; color:#e6edf3; font-size:14px; }
  button { background:#238636; border-color:#2ea043; cursor:pointer; margin-top:14px; font-weight:600; }
  button:hover { background:#2ea043; }
  button.secondary { background:#21262d; border-color:#30363d; }
  .hidden { display:none !important; }
  .msg { margin-top:12px; font-size:13px; padding:10px; border-radius:8px; }
  .msg.err { background:#3d1618; color:#ff7b72; }
  .msg.ok { background:#0f2e1a; color:#7ee787; }
  .code { font-family:monospace; font-size:15px; background:#0d1117; border:1px solid #30363d; padding:10px; border-radius:8px; margin-top:8px; user-select:all; word-break:break-all; }
  .step-title { font-size:12px; color:#8b949e; margin-bottom:4px; text-transform:uppercase; letter-spacing:1px; }
</style>
</head>
<body>
<div class="card">
  <h1>API_ID / API_HASH</h1>
  <p class="sub">Сервер сам сходит на my.telegram.org от твоего имени.</p>

  <div id="msg" class="msg hidden"></div>

  <div id="s1">
    <div class="step-title">Шаг 1 — номер</div>
    <label>Телефон</label>
    <input id="phone" placeholder="+79991234567"/>
    <button onclick="step1()">Отправить код</button>
  </div>

  <div id="s2" class="hidden">
    <div class="step-title">Шаг 2 — код из Telegram</div>
    <label>Код</label>
    <input id="code" placeholder="12345"/>
    <button onclick="step2()">Подтвердить</button>
  </div>

  <div id="s3" class="hidden">
    <div class="step-title">Шаг 3 — пароль 2FA</div>
    <label>Облачный пароль</label>
    <input id="password" type="password"/>
    <button onclick="step3()">Войти</button>
  </div>

  <div id="s4" class="hidden">
    <div class="step-title">Шаг 4 — создать приложение</div>
    <label>Название приложения</label>
    <input id="title" value="MyApp"/>
    <label>Короткое имя (только латиница, без пробелов)</label>
    <input id="shortname" value="myapp"/>
    <button onclick="createApp()">Создать приложение</button>
  </div>

  <div id="s5" class="hidden">
    <div class="step-title">Готово</div>
    <label>api_id</label>
    <div class="code" id="out-id"></div>
    <label>api_hash</label>
    <div class="code" id="out-hash"></div>
    <button class="secondary" onclick="location.reload()">Начать заново</button>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const msg = $("msg");

function show(t, err) {
  if (err === undefined) err = true;
  msg.textContent = t;
  msg.className = "msg " + (err ? "err" : "ok");
  msg.classList.remove("hidden");
}

function hideAll() {
  ["s1","s2","s3","s4","s5"].forEach(function(s){ $(s).classList.add("hidden"); });
}

async function api(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body || {})
  });
  return r.json();
}

async function step1() {
  show("Отправляю запрос на my.telegram.org...", false);
  const res = await api("/api/tg/step1", { phone: $("phone").value.trim() });
  if (!res.ok) return show(res.error);
  hideAll(); $("s2").classList.remove("hidden");
  show("Код отправлен в Telegram", false);
}

async function step2() {
  show("Проверяю код...", false);
  const res = await api("/api/tg/step2", { code: $("code").value.trim() });
  if (!res.ok) return show(res.error);
  if (res.need_password) {
    hideAll(); $("s3").classList.remove("hidden");
    return show("Нужен пароль 2FA", false);
  }
  hideAll(); $("s4").classList.remove("hidden");
  show("Вошёл", false);
}

async function step3() {
  show("Проверяю пароль...", false);
  const res = await api("/api/tg/step3", { password: $("password").value });
  if (!res.ok) return show(res.error);
  hideAll(); $("s4").classList.remove("hidden");
  show("Вошёл", false);
}

async function createApp() {
  show("Создаю приложение...", false);
  const res = await api("/api/tg/create_app", {
    title: $("title").value.trim(),
    shortname: $("shortname").value.trim(),
    platform: "desktop"
  });
  if (!res.ok) return show(res.error);
  hideAll(); $("s5").classList.remove("hidden");
  $("out-id").textContent = res.api_id;
  $("out-hash").textContent = res.api_hash;
  show("Готово! Скопируй значения.", false);
}
</script>
</body>
</html>
"""


# -----------------------------------------------------------------------------
# Routes — main page
# -----------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(PAGE)


@app.get("/api/status")
def api_status():
    with runtime.state_lock:
        auth = runtime.state["authorized"]

    if not auth and TG_API_ID and TG_API_HASH:
        session_file = SESSION_DIR / "main.session"

        if session_file.exists():
            try:
                client = runtime.get_client()
                wait_future(
                    runtime.submit(client.is_user_authorized()),
                    timeout=15,
                )
                with runtime.state_lock:
                    auth = runtime.state["authorized"]
            except Exception:
                logger.exception("Status check failed.")

    return jsonify({
        "ok": True,
        "authorized": bool(auth),
        "state": runtime.snapshot(),
        "contacts": db.count_contacts(),
    })


@app.post("/api/send_code")
def api_send_code():
    data = request.get_json(silent=True) or {}

    if not TG_API_ID or not TG_API_HASH:
        return json_error(
            "TG_API_ID / TG_API_HASH не заданы. Получи их на /getapi",
            500,
        )

    try:
        phone = validate_phone(data.get("phone"))
        wait_future(runtime.submit(runtime.send_code(phone)))
        return jsonify({"ok": True})
    except Exception as exc:
        logger.exception("Could not send authentication code.")
        runtime.update_state(auth_error=str(exc))
        return json_error(str(exc), 500)


@app.post("/api/verify_code")
def api_verify_code():
    data = request.get_json(silent=True) or {}

    try:
        phone = validate_phone(data.get("phone") or runtime.snapshot()["phone"] or "")
        code = validate_code(data.get("code"))

        wait_future(runtime.submit(runtime.sign_in_code(phone, code)))
        return jsonify({"ok": True})

    except PhoneCodeInvalidError:
        return json_error("Неверный код.", 400)

    except PhoneCodeExpiredError:
        return json_error("Код истёк.", 400)

    except SessionPasswordNeededError:
        return jsonify({"ok": True, "need_password": True})

    except Exception as exc:
        logger.exception("Code sign-in failed.")
        runtime.update_state(auth_error=str(exc))
        return json_error(str(exc), 500)


@app.post("/api/verify_password")
def api_verify_password():
    data = request.get_json(silent=True) or {}
    password = str(data.get("password", ""))

    if not password or len(password) > 500:
        return json_error("Неверный пароль.", 400)

    try:
        wait_future(runtime.submit(runtime.sign_in_password(password)))
        return jsonify({"ok": True})

    except PasswordHashInvalidError:
        return json_error("Неверный 2FA-пароль.", 400)

    except Exception as exc:
        logger.exception("Password sign-in failed.")
        runtime.update_state(auth_error=str(exc))
        return json_error(str(exc), 500)


@app.post("/api/logout")
def api_logout():
    try:
        wait_future(runtime.submit(runtime.disconnect()))
        return jsonify({"ok": True})
    except Exception as exc:
        logger.exception("Logout failed.")
        return json_error(str(exc), 500)


# -----------------------------------------------------------------------------
# Routes — settings
# -----------------------------------------------------------------------------

@app.get("/api/settings")
def api_get_settings():
    return jsonify({
        "ok": True,
        "message": db.get_setting("message", "Привет!"),
        "min_delay": float(db.get_setting("min_delay", "3")),
        "max_per_hour": int(db.get_setting("max_per_hour", "20")),
    })


@app.post("/api/settings")
def api_save_settings():
    data = request.get_json(silent=True) or {}

    if "message" in data:
        db.set_setting("message", str(data["message"]))

    if "min_delay" in data:
        try:
            db.set_setting("min_delay", str(float(data["min_delay"])))
        except (TypeError, ValueError):
            return json_error("min_delay must be a number.")

    if "max_per_hour" in data:
        try:
            db.set_setting("max_per_hour", str(int(data["max_per_hour"])))
        except (TypeError, ValueError):
            return json_error("max_per_hour must be an integer.")

    return jsonify({"ok": True})


# -----------------------------------------------------------------------------
# Routes — keywords
# -----------------------------------------------------------------------------

@app.get("/api/keywords")
def api_list_keywords():
    return jsonify({"ok": True, "words": db.get_keywords()})


@app.post("/api/keywords")
def api_add_keyword():
    data = request.get_json(silent=True) or {}

    try:
        db.add_keyword(data.get("word"))
    except ValueError as exc:
        return json_error(str(exc))

    return jsonify({"ok": True})


@app.delete("/api/keywords/by-word/<path:word>")
def api_del_keyword(word):
    db.delete_keyword(word)
    return jsonify({"ok": True})


# -----------------------------------------------------------------------------
# Routes — excluded groups
# -----------------------------------------------------------------------------

@app.get("/api/excluded")
def api_list_excluded():
    return jsonify({"ok": True, "groups": db.get_excluded_titles()})


@app.post("/api/excluded")
def api_add_excluded():
    data = request.get_json(silent=True) or {}

    try:
        db.add_excluded_title(data.get("title"))
    except ValueError as exc:
        return json_error(str(exc))

    return jsonify({"ok": True})


@app.delete("/api/excluded/by-title/<path:title>")
def api_del_excluded(title):
    db.delete_excluded_title(title)
    return jsonify({"ok": True})


# -----------------------------------------------------------------------------
# Routes — scan / progress / stop
# -----------------------------------------------------------------------------

@app.get("/api/progress")
def api_progress():
    state = runtime.snapshot()

    with runtime.state_lock:
        log_lines = list(state["last_log"])

    return jsonify({
        "ok": True,
        "running": bool(state["scan_running"]),
        "progress": state["progress"],
        "sent": state["sent_count"],
        "skipped": state["skipped_count"],
        "errors": state["error_count"],
        "log": log_lines[-50:],
    })


@app.post("/api/stop")
def api_stop():
    runtime.request_stop()
    return jsonify({"ok": True})


@app.post("/api/scan/start")
@authorized_required
def api_scan_start():
    state = runtime.snapshot()

    if state["scan_running"]:
        return json_error("Уже запущено.", 409)

    if not db.get_keywords():
        return json_error("Не добавлено ни одного ключевого слова.")

    if not db.get_setting("message"):
        return json_error("Не задан текст сообщения.")

    future = runtime.submit(runtime.scan_and_send())

    def report_failure(done):
        try:
            done.result()
        except Exception:
            logger.exception("Background scan failed.")

    future.add_done_callback(report_failure)

    return jsonify({"ok": True})


# -----------------------------------------------------------------------------
# Routes — /getapi helpers
# -----------------------------------------------------------------------------

@app.route("/getapi")
def getapi_page():
    return render_template_string(GETAPI_PAGE)


@app.post("/api/tg/step1")
def tg_step1():
    data = request.get_json(silent=True) or {}
    phone = str(data.get("phone", "")).strip()

    if not phone.startswith("+") or len(phone) < 8:
        return json_error("Номер в формате +7XXXXXXXXXX")

    try:
        s = _new_tg_session()
        s.get(MYTG + "/", timeout=20)
        r = s.post(
            MYTG + "/auth/send_password",
            data={"phone": phone},
            timeout=20,
        )

        try:
            payload = r.json()
        except Exception:
            return json_error(
                "my.telegram.org ответил не-JSON: " + r.text[:200],
                500,
            )

        if "random_hash" not in payload:
            return json_error(
                "Ошибка от my.telegram.org: " + str(payload)
            )

        sid = _get_sid()

        with _tg_sessions_lock:
            _tg_sessions[sid] = {
                "s": s,
                "phone": phone,
                "random_hash": payload["random_hash"],
                "stage": "code",
            }

        return jsonify({"ok": True})

    except Exception as exc:
        logger.exception("tg step1 failed.")
        return json_error(type(exc).__name__ + ": " + str(exc), 500)


@app.post("/api/tg/step2")
def tg_step2():
    data = request.get_json(silent=True) or {}
    code = str(data.get("code", "")).strip()
    sid = _get_sid()

    with _tg_sessions_lock:
        st = _tg_sessions.get(sid)

    if not st or st.get("stage") != "code":
        return json_error("Сессия истекла, начните заново.")

    try:
        s = st["s"]
        r = s.post(
            MYTG + "/auth/login",
            data={
                "phone": st["phone"],
                "random_hash": st["random_hash"],
                "password": code,
            },
            timeout=20,
        )

        text = r.text.strip()

        if text == "true":
            st["stage"] = "logged_in"
            return jsonify({"ok": True})

        low = text.lower()

        if "password" in low or "two" in low or "2fa" in low:
            st["stage"] = "password"
            return jsonify({"ok": True, "need_password": True})

        return json_error("Ответ my.telegram.org: " + text[:200])

    except Exception as exc:
        logger.exception("tg step2 failed.")
        return json_error(type(exc).__name__ + ": " + str(exc), 500)


@app.post("/api/tg/step3")
def tg_step3():
    data = request.get_json(silent=True) or {}
    password = str(data.get("password", ""))
    sid = _get_sid()

    with _tg_sessions_lock:
        st = _tg_sessions.get(sid)

    if not st or st.get("stage") != "password":
        return json_error("Сессия истекла.")

    try:
        s = st["s"]
        r = s.post(
            MYTG + "/auth/login",
            data={"password": password},
            timeout=20,
        )

        if r.text.strip() == "true":
            st["stage"] = "logged_in"
            return jsonify({"ok": True})

        return json_error("Ответ: " + r.text[:200])

    except Exception as exc:
        logger.exception("tg step3 failed.")
        return json_error(type(exc).__name__ + ": " + str(exc), 500)


@app.post("/api/tg/create_app")
def tg_create_app():
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "MyApp").strip() or "MyApp"
    shortname = str(data.get("shortname") or "myapp").strip() or "myapp"
    platform = str(data.get("platform") or "desktop")
    sid = _get_sid()

    with _tg_sessions_lock:
        st = _tg_sessions.get(sid)

    if not st or st.get("stage") != "logged_in":
        return json_error("Не авторизован.")

    try:
        s = st["s"]
        r = s.get(MYTG + "/apps", timeout=20)
        soup = BeautifulSoup(r.text, "html.parser")
        hash_input = soup.find("input", {"name": "hash"})
        final_text = ""

        if hash_input:
            app_hash = hash_input.get("value", "")
            r2 = s.post(
                MYTG + "/apps",
                data={
                    "hash": app_hash,
                    "app_title": title,
                    "app_shortname": shortname,
                    "app_url": "",
                    "app_platform": platform,
                    "app_desc": "",
                },
                timeout=20,
            )
            final_text = r2.text
        else:
            final_text = r.text

        api_id = None
        api_hash = None

        m1 = re.search(
            r"api_id[^\d]{0,20}(\d{5,})",
            final_text,
            re.IGNORECASE,
        )
        m2 = re.search(
            r"api_hash[^a-f0-9]{0,20}([a-f0-9]{32})",
            final_text,
            re.IGNORECASE,
        )

        if m1:
            api_id = m1.group(1)
        if m2:
            api_hash = m2.group(1)

        if not api_id or not api_hash:
            codes = BeautifulSoup(final_text, "html.parser").find_all("code")

            for cc in codes:
                t = cc.get_text(strip=True)

                if t.isdigit() and len(t) >= 6 and not api_id:
                    api_id = t
                elif (
                    len(t) == 32
                    and all(ch in "0123456789abcdef" for ch in t.lower())
                    and not api_hash
                ):
                    api_hash = t

        if not api_id or not api_hash:
            return json_error(
                "Не удалось извлечь ключи. Возможно, приложение уже "
                "есть с другим именем — зайди на my.telegram.org "
                "вручную или смени title/shortname и попробуй снова.",
                500,
            )

        return jsonify({"ok": True, "api_id": api_id, "api_hash": api_hash})

    except Exception as exc:
        logger.exception("tg create_app failed.")
        return json_error(type(exc).__name__ + ": " + str(exc), 500)


# -----------------------------------------------------------------------------
# Startup
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("Data dir: %s", DATA_DIR)
    logger.info("Database: %s", DB_PATH)
    logger.info("Telegram session: %s", SESSION_PATH)
    logger.info("Web interface: http://%s:%s", HOST, PORT)

    app.run(
        host=HOST,
        port=PORT,
        debug=False,
        threaded=True,
    )
