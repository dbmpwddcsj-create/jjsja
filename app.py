import os
import re
import time
import uuid as _uuid
import asyncio
import sqlite3
import threading
import traceback
from datetime import datetime
from flask import Flask, request, jsonify, session, render_template_string
from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError,
    UserPrivacyRestrictedError, PeerFloodError,
    UserNotMutualContactError, InputUserDeactivatedError,
)
from telethon.tl.types import User
import requests as _requests
from bs4 import BeautifulSoup

# ============================================================
# КОНФИГ
# ============================================================
API_ID = int(os.environ.get("TG_API_ID", "0"))
API_HASH = os.environ.get("TG_API_HASH", "")

def _pick_data_dir():
    preferred = os.environ.get("DATA_DIR", "").strip()
    candidates = []
    if preferred:
        candidates.append(preferred)
    candidates += ["/var/data", "./data", "/tmp/data"]
    for path in candidates:
        try:
            os.makedirs(path, exist_ok=True)
            test = os.path.join(path, ".writable_test")
            with open(test, "w") as f:
                f.write("ok")
            os.remove(test)
            return path
        except Exception:
            continue
    raise RuntimeError("Не нашёл папку, куда можно писать")

DATA_DIR = _pick_data_dir()
SESSION_DIR = os.path.join(DATA_DIR, "sessions")
DB_PATH = os.path.join(DATA_DIR, "app.db")
os.makedirs(SESSION_DIR, exist_ok=True)
print(f"[startup] DATA_DIR = {DATA_DIR}", flush=True)
print(f"[startup] API_ID   = {API_ID}", flush=True)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", os.urandom(32).hex())

# ============================================================
# БАЗА
# ============================================================
state_lock = threading.Lock()
db_write_lock = threading.Lock()

def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE IF NOT EXISTS keywords (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        word TEXT UNIQUE NOT NULL
    );
    CREATE TABLE IF NOT EXISTS excluded_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT UNIQUE NOT NULL
    );
    CREATE TABLE IF NOT EXISTS contacted (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        group_title TEXT,
        sent_at TEXT,
        status TEXT
    );
    """)
    c.commit()
    c.close()

init_db()

def get_setting(key, default=None):
    c = db()
    row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    c.close()
    return row["value"] if row else default

def set_setting(key, value):
    with db_write_lock:
        c = db()
        c.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value))
        )
        c.commit()
        c.close()

# ============================================================
# СОСТОЯНИЕ
# ============================================================
state = {
    "client": None,
    "loop": None,
    "authorized": False,
    "running": False,
    "stop_flag": False,
    "progress": "",
    "sent_count": 0,
    "skipped_count": 0,
    "error_count": 0,
    "last_log": [],
}

def log(msg):
    with state_lock:
        state["last_log"].append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")
        state["last_log"] = state["last_log"][-200:]

# ============================================================
# EVENT LOOP TELETHON
# ============================================================
_loop = asyncio.new_event_loop()
def _loop_runner():
    asyncio.set_event_loop(_loop)
    = _loop.run_forever()
threading.Thread(target=_loop_runner, daemon=True).start()
state["loop"] = _loop

def run_async(coro, timeout=300):
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=timeout)

def get_client():
    with state_lock:
 p        if state["client"] is not None.errors:
            return state["client"]
        session_path = os.path.join;
(SESSION_DIR, "main")
         client = TelegramClient(session_path, API_ID, $ API_HASH, loop=_loop)
        run_async(client.connect(), timeout=60)
        state["client"] = client
        return client

# ============================================================
# HTML — ГЛАВНАЯ
# ============================================================
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
  button.secondary { background:#21262d; border-color:#30363d; }
  button.danger { background:#8b1a1a; border-color:#b62324; }
  button.small { width:auto; padding:6px 10px; font-size:12px; margin:0 0 0 8px; }
  .hidden { display:none !important; }
  .msg { margin-top:12px; font-size:13px; padding:10px; border-radius:8px; }
  .msg.err { background:#3d1618; color:#ff7b72; }
  .msg.ok { background:#0f2e1a; color:#7ee787; }
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
  <a class="getapi" href="/getapi" target="_blank">→ Получить api_id / api_hash</a>
  <div id="msg" class="msg hidden"></div>

  <div id="step-phone" class="card">
    <div class="step-title">Шаг 1 — телефон</div>
    <label>Номер (в формате +7XXXXXXXXXX)</label>
    <input id="phone" placeholder="+79991234567"/>
    <button onclick="sendCode()">Отправить код</button>
  </div>

  <div id="step-code" class="card hidden">
    <div class="step-title">Шаг 2 — код из Telegram</div>
    <label>Код</label>
    <input id="code" placeholder="12345"/>
    <button onclick="verifyCode()">Подтвердить</button>
  </div>

  <div id="step-password" class="card hidden">
    <div class="step-title">Шаг 3 — пароль 2FA</div>
    <label>Облачный пароль</label>
    <input id="password" type="password"/>
    <button onclick="verifyPassword()">Войти</button>
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
const $ = id => document.getElementById(id);
const msg = $("msg");
let pollTimer = null;

function show(t, err=true) {
  msg.textContent = t;
  msg.className = "msg " + (err ? "err" : "ok");
  msg.classList.remove("hidden");
  setTimeout(()=>msg.classList.add("hidden"), 6000);
}
async function api(url, body, method="POST") {
  const r = await fetch(url, {
    method,
    headers: {"Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined,
  });
  return r.json();
}
function showAuth(id) {
  ["step-phone","step-code","step-password"].forEach(s => $(s).classList.add("hidden"));
  if (id) $(id).classList.remove("hidden");
}

async function sendCode() {
  const phone = $("phone").value.trim();
  const res = await api("/api/send_code", { phone });
  if (!res.ok) return show(res.error);
  if (res.already_authorized) return openPanel();
  showAuth("step-code");
}
async function verifyCode() {
  const code = $("code").value.trim();
  const res = await api("/api/verify_code", { code });
  if (!res.ok) return show(res.error);
  if (res.need_password) return showAuth("step-password");
  openPanel();
}
async function verifyPassword() {
  const password = $("password").value;
  const res = await api("/api/verify_password", { password });
  if (!res.ok) return show(res.error);
  openPanel();
}
async function logout() {
  await api("/api/logout");
  location.reload();
}

async function openPanel() {
  showAuth(null);
  $("panel").classList.remove("hidden");
  await loadSettings();
  await loadKw();
  await loadEx();
  startPolling();
}

async function loadSettings() {
  const s = await api("/api/settings", null, "GET");
  $("message").value = s.message || "";
  $("min_delay").value = s.min_delay;
  $("max_per_hour").value = s.max_per_hour;
}
async function saveSettings() {
  await api("/api/settings", {
    message: $("message").value,
    min_delay: parseFloat($("min_delay").value || "3"),
    max_per_hour: parseInt($("max_per_hour").value || "20"),
  });
  show("Настройки сохранены", false);
}

async function loadKw() {
  const r = await api("/api/keywords", null, "GET");
  const box = $("kw-list");
  box.innerHTML = "";
  r.words.forEach(w => {
    const el = document.createElement("span");
    el.className = "tag";
    const btn = document.createElement("button");
    btn.textContent = "×";
    btn.onclick = () => delKw(w);
    el.textContent = w;
    el.appendChild(btn);
    box.appendChild(el);
  });
}
async function addKw() {
  const w = $("kw-input").value.trim();
  if (!w) return;
  await api("/api/keywords", { word: w });
  $("kw-input").value = "";
  loadKw();
}
async function delKw(word) {
  await fetch("/api/keywords/by-word/" + encodeURIComponent(word), {method:"DELETE"});
  loadKw();
}

async function loadEx() {
  const r = await api("/api/excluded", null, "GET");
  const box = $("ex-list");
  box.innerHTML = "";
  r.groups.forEach(g => {
    const el = document.createElement("span");
    el.className = "tag";
    const btn = document.createElement("button");
    btn.textContent = "×";
    btn.onclick = () => delEx(g);
    el.textContent = g;
    el.appendChild(btn);
    box.appendChild(el);
  });
}
async function addEx() {
  const t = $("ex-input").value.trim();
  if (!t) return;
  await api("/api/excluded", { title: t });
  $("ex-input").value = "";
  loadEx();
}
async function delEx(title) {
  await fetch("/api/excluded/by-title/" + encodeURIComponent(title), {method:"DELETE"});
  loadEx();
}

async function startScan() {
  const res = await api("/api/scan/start");
  if (!res.ok) return show(res.error);
  $("btn-start").classList.add("hidden");
  $("btn-stop").classList.remove("hidden");
  startPolling();
}
async function stopScan() {
  await api("/api/stop");
}
function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(pollProgress, 1500);
  pollProgress();
}
async function pollProgress() {
  const p = await api("/api/progress", null, "GET");
  $("c-sent").textContent = p.sent;
  $("c-skip").textContent = p.skipped;
  $("c-err").textContent("progress").textContent = (p.progress ? p.progress + "\n\n" : "") + (p.log || []).join("\n");
  if (p.running) {
    $("btn-start").classList.add("hidden");
    $("btn-stop").classList.remove("hidden");
  } else {
    $("btn-start").classList.remove("hidden");
    $("btn-stop").classList.add("hidden");
  }
}

(async () => {
  const s = await api("/api/status", null, "GET");
  if (s.authorized) openPanel();
})();
</script>
</body>
</html>
"""

# ============================================================
# HTML — /getapi
# ============================================================
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
function show(t, err=true) {
  msg.textContent = t;
  msg.className = "msg " + (err ? "err" : "ok");
  msg.classList.remove("hidden");
}
function hideAll() { ["s1","s2","s3","s4","s5"].forEach(s => $(s).classList.add("hidden")); }
async function api(url, body) {
  const r = await fetch(url, { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body||{}) });
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
  if (res.need_password) { hideAll(); $("s3").classList.remove("hidden"); return show("Нужен пароль 2FA", false); }
  hideAll(); $("s4").classList.remove("hidden"); show("Вошёл", false);
}
async function step3() {
  show("Проверяю пароль...", false);
  const res = await api("/api/tg/step3", { password: $("password").value });
  if (!res.ok) return show(res.error);
  hideAll(); $("s4").classList.remove("hidden"); show("Вошёл", false);
}
async function createApp() {
  show("Создаю приложение...", false);
  const res = await api("/api/tg/create_app", {
    title: $("title").value.trim(),
    shortname: $("shortname").value.trim(),
    platform: "desktop",
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

# ============================================================
# РОУТЫ — ГЛАВНАЯ
# ============================================================

@app.route("/")
def index():
    return render_template_string(PAGE)


# ---- АВТОРИЗАЦИЯ ----

@app.route("/api/send_code", methods=["POST"])
def send_code():
    phone = (request.json or {}).get("phone", "").strip()
    if not phone:
        return jsonify(ok=False, error="Введите номер"), 400
    if not API_ID or not API_HASH:
        return jsonify(ok=False, error="TG_API_ID / TG_API_HASH не заданы в переменных окружения Render. Получи их на /getapi"), 500
    try:
        client = get_client()
        if run_async(client.is_user_authorized(), timeout=30):
            with state_lock:
                state["authorized"] = True
            return jsonify(ok=True, already_authorized=True)
        sent = run_async(client.send_code_request(phone), timeout=60)
        session["phone"] = phone
        session["phone_code_hash"] = sent.phone_code_hash
        return jsonify(ok=True)
    except Exception as e:
        traceback.print_exc()
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500


@app.route("/api/verify_code", methods=["POST"])
def verify_code():
    data = request.json or {}
    phone = session.get("phone")
    code = (data.get("code") or "").strip()
    phone_code_hash = session.get("phone_code_hash")
    if not phone or not code:
        return jsonify(ok=False, error="Нет сессии или пустой код"), 400
    try:
        client = get_client()
        run_async(client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash), timeout=60)
        with state_lock:
            state["authorized"] = True
        return jsonify(ok=True)
    except SessionPasswordNeededError:
        return jsonify(ok=True, need_password=True)
    except PhoneCodeInvalidError:
        return jsonify(ok=False, error="Неверный код"), 400
    except PhoneCodeExpiredError:
        return jsonify(ok=False, error="Код истёк"), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500


@app.route("/api/verify_password", methods=["POST"])
def verify_password():
    password = (request.json or {}).get("password") or ""
    if not password:
        return jsonify(ok=False, error="Пустой пароль"), 400
    try:
        client = get_client()
        run_async(client.sign_in(password=password), timeout=60)
        with state_lock:
            state["authorized"] = True
        return jsonify(ok=True)
    except Exception as e:
        traceback.print_exc()
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500


@app.route("/api/status")
def status():
    with state_lock:
        auth = state["authorized"]
    if not auth and API_ID and API_HASH:
        try:
            client = get_client()
            if run_async(client.is_user_authorized(), timeout=10):
                with state_lock:
                    state["authorized"] = True
                auth = True
        except Exception:
            pass
    return jsonify(authorized=auth)


@app.route("/api/logout", methods=["POST"])
def logout():
    try:
        client = get_client()
        run_async(client.log_out(), timeout=30)
    except Exception:
        pass
    with state_lock:
        state["client"] = None
        state["authorized"] = False
    return jsonify(ok=True)


# ---- НАСТРОЙКИ ----

@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify(
        message=get_setting("message", "Привет!"),
        min_delay=float(get_setting("min_delay", "3")),
        max_per_hour=int(get_setting("max_per_hour", "20")),
    )


@app.route("/api/settings", methods=["POST"])
def save_settings():
    d = request.json or {}
    if "message" in d: set_setting("message", d["message"])
    if "min_delay" in d: set_setting("min_delay", str(d["min_delay"]))
    if "max_per_hour" in d: set_setting("max_per_hour", str(d["max_per_hour"]))
    return jsonify(ok=True)


# ---- КЛЮЧЕВЫЕ СЛОВА ----

@app.route("/api/keywords", methods=["GET"])
def list_keywords():
    c = db()
    rows = c.execute("SELECT word FROM keywords ORDER BY word").fetchall()
    c.close()
    return jsonify(words=[r["word"] for = r in rows])


@app.route("/api/keywords", methods=["POST"])
def add_keyword now():
    word = (request.json or {}).get("word",
 "").strip()
    if not word:
        return jsonify                   (ok=False, error="Пустое слово sent"), 400
    with db_write_lock:
_in        c = db()
        try:
            c.execute("INSERT INTO keywords(word) VALUES(?)", (word,))
            c.commit()
        except sqlite3.IntegrityError:
            pass
        c.close()
    return jsonify(ok=True)


@app.route("/api/keywords/by-word/<path:word>", methods=["DELETE"])
def del_keyword_by_word(word):
    with db_write_lock:
        c = db()
        c.execute("DELETE FROM keywords WHERE word=?", (word,))
        c.commit()
        c.close()
    return jsonify(ok=True)


# ---- ИСКЛЮЧЕНИЯ ----

@app.route("/api/excluded", methods=["GET"])
def list_excluded():
    c = db()
    rows = c.execute("SELECT title FROM excluded_groups ORDER BY title").fetchall()
    c.close()
    return jsonify(groups=[r["title"] for r in rows])


@app.route("/api/excluded", methods=["POST"])
def add_excluded():
    title = (request.json or {}).get("title", "").strip()
    if not title:
        return jsonify(ok=False, error="Пустое название"), 400
    with db_write_lock:
        c = db()
        try:
            c.execute("INSERT INTO excluded_groups(title) VALUES(?)", (title,))
            c.commit()
        except sqlite3.IntegrityError:
            pass
        c.close()
    return jsonify(ok=True)


@app.route("/api/excluded/by-title/<path:title>", methods=["DELETE"])
def del_excluded_by_title(title):
    with db_write_lock:
        c = db()
        c.execute("DELETE FROM excluded_groups WHERE title=?", (title,))
        c.commit()
        c.close()
    return jsonify(ok=True)


# ---- ПРОГРЕСС / СТОП ----

@app.route("/api/progress")
def progress():
    with state_lock:
        return jsonify(
            running=state["running"],
            progress=state["progress"],
            sent=state["sent_count"],
            skipped=state["skipped_count"],
            errors=state["error_count"],
            log=state["last_log"][-50:],
        )


@app.route("/api/stop", methods=["POST"])
def stop():
    with state_lock:
        state["stop_flag"] = True
    return jsonify(ok=True)


# ============================================================
# СКАНИРОВАНИЕ
# ============================================================

def check_ready():
    if not get_setting("message"):
        return "Не задан текст сообщения"
    c = db()
    kw = c.execute("SELECT COUNT(*) as n FROM keywords").fetchone()["n"]
    c.close()
    if kw == 0:
        return "Не добавлено ни одного ключевого слова"
    if not state["authorized"]:
        return "Аккаунт не авторизован"
    return None


@app.route("/api/scan/start", methods=["POST"])
def scan_start():
    err = check_ready()
    if err:
        return jsonify(ok=False, error=err), 400
    with state_lock:
        if state["running"]:
            return jsonify(ok=False, error="Уже запущено"), 400
        state["running"] = True
        state["stop_flag"] = False
        state["sent_count"] = 0
        state["skipped_count"] = 0
        state["error_count"] = 0
        state["progress"] = "Запуск..."
        state["last_log"] = []
    threading.Thread(target=scan_worker, daemon=True).start()
    return jsonify(ok=True)


def scan_worker():
    try:
        run_async(_scan_and_send(), timeout=60 * 60 * 6)
    except Exception as e:
        traceback.print_exc()
        log(f"Фатальная ошибка: {type(e).__name__}: {e}")
    finally:
        with state_lock:
            state["running"] = False
            state["progress"] = "Остановлено"


def _save_contact(uid, username, first_name, group_title, status):
    with db_write_lock:
        c = db()
        try:
            c.execute(
                "INSERT OR IGNORE INTO contacted(user_id,username,first_name,group_title,sent_at,status) VALUES(?,?,?,?,?,?)",
                (uid, username, first_name, group_title, datetime.now().isoformat(), status)
            )
            c.commit()
        finally:
            c.close()


async def _scan_and_send():
    client = get_client()
    message_text = get_setting("message", "")
    min_delay = float(get_setting("min_delay", "3"))
    max_per_hour = int(get_setting("max_per_hour", "20"))

    c = db()
    keywords = [r["word"].lower() for r in c.execute("SELECT word FROM keywords").fetchall()]
    excluded = {r["title"].lower() for r in c.execute("SELECT title FROM excluded_groups").fetchall()}
    contacted_ids = {r["user_id"] for r in c.execute("SELECT user_id FROM contacted").fetchall()}
    c.close()

    if not keywords:
        log("Нет ключевых слов, стоп")
        return

    log("Получаю список групп...")
    dialogs = await client.get_dialogs()
    groups = [d for d in dialogs if d.is_group]
    total_groups = len(groups)
    log(f"Найдено групп: {total_groups}")

    hour_window_start = time.time()
    sent_in_window = 0

    for g_idx, dialog in enumerate(groups, 1):
        if state["stop_flag"]:
            log("Остановка по запросу")
            return
        title = dialog.title or ""
        if title.lower() in excluded:
            log(f"[{g_idx}/{total_groups}] Пропуск исключённой: {title}")
            continue

        with state_lock:
            state["progress"] = f"Скан группы {g_idx}/{total_groups}: {title}"
        log(f"[{g_idx}/{total_groups}] Сканирую: {title}")

        try:
            async for msg in client.iter_messages(dialog.id):
                if state["stop_flag"]:
                    log("Остановка по запросу")
                    return
                if not msg.sender_id:
                    continue
                text = (msg.message or "").lower()
                if not text:
                    continue
                if not any(kw in text for kw in keywords):
                    continue

                try:
                    sender = await msg.get_sender()
                except Exception:
                    continue
                if not isinstance(sender, User):
                    continue
                if sender.bot or sender.is_self:
                    continue

                uid = sender.id
                if uid in contacted_ids:
                    with state_lock:
                        state["skipped_count"] += 1
                    continue

                now = time.time()
                if now - hour_window_start >= 3600:
                    hour_window_start_window = 0
                if sent_in_window >= max_per_hour:
                    wait = 3600 - (now - hour_window_start)
                    if wait > 0:
                        log(f"Лимит {max_per_hour}/час. Пауза {int(wait)} сек...")
                        await asyncio.sleep(wait)
                        hour_window_start = time.time()
                        sent_in_window = 0

                try:
                    history = await client.get_messages(sender, limit=1)
                    if history and len(history) > 0:
                        with state_lock:
                            state["skipped_count"] += 1
                        _save_contact(uid, sender.username or "", sender.first_name or "", title, "skipped_had_history")
                        contacted_ids.add(uid)
                        continue
                except Exception as e:
                    log(f"  Ошибка проверки истории {uid}: {e}")
                    continue

                try:
                    await client.send_message(sender, message_text)
                    sent_in_window += 1
                    with state_lock:
                        state["sent_count"] += 1
                    log(f"  ✅ Отправлено: {sender.first_name} (@{sender.username or uid})")
                    _save_contact(uid, sender.username or "", sender.first_name or "", title, "sent")
                    contacted_ids.add(uid)
                except (UserPrivacyRestrictedError, UserNotMutualContactError) as e:
                    log(f"  ⛔ Приватность: {uid} ({type(e).__name__})")
                    with state_lock:
                        state["error_count"] += 1
                    _save_contact(uid, sender.username or "", sender.first_name or "", title, "privacy_blocked")
                    contacted_ids.add(uid)
                except PeerFloodError:
                    log("  ⚠️ PeerFloodError — Telegram ограничил отправку. Стоп.")
                    return
                except InputUserDeactivatedError:
                    log(f"  Аккаунт {uid} удалён")
                    with state_lock:
                        state["error_count"] += 1
                except Exception as e:
                    log(f"  ❌ Ошибка отправки {uid}: {type(e).__name__}: {e}")
                    with state_lock:
                        state["error_count"] += 1

                await asyncio.sleep(min_delay)

        except Exception as e:
            log(f"  Ошибка сканирования {title}: {type(e).__name__}: {e}")
            continue

    log("✅ Готово")


# ============================================================
# my.telegram.org — ПОЛУЧЕНИЕ api_id / api_hash
# ============================================================
MYTG = "https://my.telegram.org"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_tg_sessions = {}

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


@app.route("/getapi")
def getapi_page():
    return render_template_string(GETAPI_PAGE)


@app.route("/api/tg/step1", methods=["POST"])
def tg_step1():
    phone = (request.json or {}).get("phone", "").strip()
    if not phone.startswith("+") or len(phone) < 8:
        return jsonify(ok=False, error="Номер в формате +7XXXXXXXXXX"), 400
    try:
        s = _new_tg_session()
        s.get(MYTG + "/", timeout=20)
        r = s.post(MYTG + "/auth/send_password", data={"phone": phone}, timeout=20)
        try:
            data = r.json()
        except Exception:
            return jsonify(ok=False, error=f"my.telegram.org ответил не-JSON: {r.text[:200]}"), 500
        if "random_hash" not in data:
            return jsonify(ok=False, error=f"Ошибка от my.telegram.org: {data}"), 400
        sid = _get_sid()
        _tg_sessions[sid] = {
            "s": s, "phone": phone,
            "random_hash": data["random_hash"], "stage": "code"
        }
        return jsonify(ok=True)
    except Exception as e:
        traceback.print_exc()
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500


@app.route("/api/tg/step2", methods=["POST"])
def tg_step2():
    code = (request.json or {}).get("code", "").strip()
    sid = _get_sid()
    st = _tg_sessions.get(sid)
    if not st or st.get("stage") != "code":
        return jsonify(ok=False, error="Сессия истекла, начните заново"), 400
    try:
        s = st["s"]
        r = s.post(MYTG + "/auth/login", data={
            "phone": st["phone"],
            "random_hash": st["random_hash"],
            "password": code,
        }, timeout=20)
        text = r.text.strip()
        if text == "true":
            st["stage"] = "logged_in"
            return jsonify(ok=True)
        low = text.lower()
        if "password" in low or "two" in low or "2fa" in low:
            st["stage"] = "password"
            return jsonify(ok=True, need_password=True)
        return jsonify(ok=False, error=f"Ответ my.telegram.org: {text[:200]}"), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500


@app.route("/api/tg/step3", methods=["POST"])
def tg_step3():
    password = (request.json or {}).get("password", "")
    sid = _get_sid()
    st = _tg_sessions.get(sid)
    if not st or st.get("stage") != "password":
        return jsonify(ok=False, error="Сессия истекла"), 400
    try:
        s = st["s"]
        r = s.post(MYTG + "/auth/login", data={"password": password}, timeout=20)
        if r.text.strip() == "true":
            st["stage"] = "logged_in"
            return jsonify(ok=True)
        return jsonify(ok=False, error=f"Ответ: {r.text[:200]}"), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500


@app.route("/api/tg/create_app", methods=["POST"])
def tg_create_app():
    d = request.json or {}
    title = (d.get("title") or "MyApp").strip() or "MyApp"
    shortname = (d.get("shortname") or "myapp").strip() or "myapp"
    platform = d.get("platform") or "desktop"
    sid = _get_sid()
    st = _tg_sessions.get(sid)
    if not st or st.get("stage") != "logged_in":
        return jsonify(ok=False, error="Не авторизован"), 400
    try:
        s = st["s"]
        r = s.get(MYTG + "/apps", timeout=20)
        soup = BeautifulSoup(r.text, "html.parser")
        hash_input = soup.find("input", {"name": "hash"})
        final_text = ""
        if hash_input:
            app_hash = hash_input.get("value", "")
            r2 = s.post(MYTG + "/apps", data={
                "hash": app_hash,
                "app_title": title,
                "app_shortname": shortname,
                "app_url": "",
                "app_platform": platform,
                "app_desc": "",
            }, timeout=20)
            final_text = r2.text
        else:
            final_text = r.text

        api_id = None
        api_hash = None
        m1 = re.search(r"api_id[^\d]{0,20}(\d{5,})", final_text, re.IGNORECASE)
        m2 = re.search(r"api_hash[^a-f0-9]{0,20}([a-f0-9]{32})", final_text, re.IGNORECASE)
        if m1: api_id = m1.group(1)
        if m2: api_hash = m2.group(1)

        if not api_id or not api_hash:
            codes = BeautifulSoup(final_text, "html.parser").find_all("code")
            for c in codes:
                t = c.get_text(strip=True)
                if t.isdigit() and len(t) >= 6 and not api_id:
                    api_id = t
                elif len(t) == 32 and all(ch in "0123456789abcdef" for ch in t.lower()) and not api_hash:
                    api_hash = t

        if not api_id or not api_hash:
            return jsonify(
                ok=False,
                error="Не удалось извлечь ключи. Возможно, приложение уже есть с другим именем — зайди на my.telegram.org вручную, или смени title/shortname и попробуй снова."
            ), 500
        return jsonify(ok=True, api_id=api_id, api_hash=api_hash)
    except Exception as e:
        traceback.print_exc()
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500


# ============================================================
# ЗАПУСК
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
