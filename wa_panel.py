"""
WhatsAppTransporter - WhatsApp Web gonderim uygulamasi (Flask + Playwright)

Kurulum:
    baslat.bat gerekli paketleri ve Chromium'u ilk calistirmada kurar.

Calistirma:
    python wa_panel.py
Panel http://127.0.0.1:5001 adresinde acilir. Ilk calistirmada acilan
WhatsApp penceresinde QR kodu telefonundan okut. Pencereyi kapatma.

CSV listesi: Google Contacts disa aktarma dosyasi (Phone 1 - Value) veya
sutunlar telefon, isim, mesaj (sadece telefon zorunlu). Ayirici , veya ; olabilir.

Liste detayi: her gorevin yanindaki "Detay" dugmesi alinan listeyi, gonderim
durumlarini, gecersiz numaralari ve CSV'de atlanan satirlari gosterir.
"""

import base64
import csv
from contextlib import contextmanager
import hashlib
import io
import json
import mimetypes
import os
import random
import re
import shutil
import sqlite3
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context
from playwright.sync_api import sync_playwright
from waitress import serve
from csv_import import normalize_phone, parse_recipients, parse_csv as _parse_csv

BASE = Path(__file__).parent
PROFILE_DIR = str(BASE / "wa_profile")
JOBS_FILE = BASE / "jobs.json"
HISTORY_DB = BASE / "sent_history.sqlite3"
IMAGES_DIR = BASE / "images"  # mesajlara eklenen resimler
SHOT_DIR = BASE / "hata_ekran"  # resim gonderimi basarisiz olursa ekran goruntusu
WA_URL = "https://web.whatsapp.com"
INPUT_SELECTOR = 'footer div[contenteditable="true"]'
DIALOG_SELECTOR = 'div[role="dialog"], div[data-animate-modal-popup="true"]'
CHATLIST_SELECTOR = "#pane-side"

# Resim onizleme ekranindaki "gonder" dugmesi (ana mesaj kutusu bos iken yalnizca onizlemede gorunur)
PREVIEW_SEND_SELECTOR = (
    'span[data-icon="send"], span[data-icon="wds-ic-send-filled"], '
    'div[role="button"][aria-label="Send"], div[role="button"][aria-label="Gönder"], '
    'button[aria-label="Send"], button[aria-label="Gönder"]'
)

# Resmi mesaj kutusuna Ctrl+V ile yapistirmis gibi gosterir
PASTE_JS = """
async ({sel, b64, mime, name}) => {
  const el = document.querySelector(sel);
  if (!el) return false;
  el.focus();
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const dt = new DataTransfer();
  dt.items.add(new File([bytes], name, {type: mime}));
  el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
  return true;
}
"""

IMAGE_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpg|gif|webp)$")

# Google Kisiler disa aktarma etiketleri (Google'da bu etikete gore filtreleyebilirsin)
LBL_INVALID = "WA Gecersiz Numara"
LBL_ERROR = "WA Gonderim Hatasi"
LBL_FIXED = "WA Numara Duzeltildi"
LBL_DUP = "WA Tekrar Kisi"
LBL_SENT_BEFORE = "WA Ayni Mesaj Daha Once Gonderildi"

# Google Kisiler iceri aktarma sablonunun basligi (Google dosyasi yuklenmediyse bu kullanilir)
GOOGLE_COLS = [
    "Name Prefix", "First Name", "Middle Name", "Last Name", "Name Suffix",
    "Phonetic First Name", "Phonetic Middle Name", "Phonetic Last Name", "Nickname", "File As",
    "E-mail 1 - Label", "E-mail 1 - Value", "Phone 1 - Label", "Phone 1 - Value",
    "Address 1 - Label", "Address 1 - Country", "Address 1 - Street", "Address 1 - Extended Address",
    "Address 1 - City", "Address 1 - Region", "Address 1 - Postal Code", "Address 1 - PO Box",
    "Organization Name", "Organization Title", "Organization Department", "Birthday",
    "Event 1 - Label", "Event 1 - Value", "Relation 1 - Label", "Relation 1 - Value",
    "Website 1 - Label", "Website 1 - Value", "Custom Field 1 - Label", "Custom Field 1 - Value",
    "Notes", "Labels",
]

# A few worker paths log while already holding the state lock.
lock = threading.RLock()
_log_schema_lock = threading.Lock()
_log_table_ready = False
_instance_lock_handle = None
state = {"status": "Baslatiliyor...", "log": []}
jobs = []
sent_message_map = {}


def log(msg, *, level="INFO", event="runtime", job_id="", phone=""):
    created_at = datetime.now().isoformat(timespec="seconds")
    line = f"{datetime.now():%d.%m %H:%M:%S}  {msg}"
    print(line)
    with lock:
        state["log"].append(line)
        state["log"] = state["log"][-100:]
    try:
        ensure_log_schema()
        with history_db() as conn:
            conn.execute("""INSERT INTO app_logs
                (created_at, level, event, job_id, phone, message) VALUES (?, ?, ?, ?, ?, ?)""",
                (created_at, level, event, job_id or "", phone or "", str(msg)))
    except Exception as exc:
        print(f"Log veritabanina yazilamadi: {exc}")


def ensure_log_schema():
    global _log_table_ready
    if _log_table_ready:
        return
    with _log_schema_lock:
        if _log_table_ready:
            return
        with history_db() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS app_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL, level TEXT NOT NULL DEFAULT 'INFO',
                event TEXT NOT NULL DEFAULT 'runtime', job_id TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '', message TEXT NOT NULL
            )""")
        _log_table_ready = True


def acquire_single_instance():
    """Browser profile and local database are owned by one app process at a time."""
    global _instance_lock_handle
    lock_path = BASE / "wa_panel.lock"
    handle = lock_path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, OSError):
        handle.close()
        return False
    _instance_lock_handle = handle
    return True


def save_jobs():
    payload = json.dumps(jobs, ensure_ascii=False, indent=2)
    temporary = JOBS_FILE.with_name(JOBS_FILE.name + ".tmp")
    backup = JOBS_FILE.with_name(JOBS_FILE.name + ".bak")
    JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        if JOBS_FILE.exists():
            try:
                json.loads(JOBS_FILE.read_text(encoding="utf-8"))
                backup_tmp = backup.with_name(backup.name + ".tmp")
                shutil.copyfile(JOBS_FILE, backup_tmp)
                os.replace(backup_tmp, backup)
            except (OSError, ValueError, json.JSONDecodeError):
                # Gecersiz ana dosya varsa saglam yedegin uzerine yazma.
                pass
        os.replace(temporary, JOBS_FILE)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def history_db():
    conn = sqlite3.connect(HISTORY_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


@lru_cache(maxsize=512)
def image_fingerprint(name):
    if not name:
        return ""
    try:
        return hashlib.sha256((IMAGES_DIR / name).read_bytes()).hexdigest()
    except OSError:
        return name


def message_fingerprint(message, image=""):
    payload = json.dumps([message or "", image_fingerprint(image)], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_csv(raw_bytes):
    return _parse_csv(raw_bytes, message_fingerprint)


def message_identity(message_id, message_key):
    """Kayitli mesaj ID'sini tekrar gonderim icin asil kimlik yapar."""
    return message_id or f"legacy:{message_key}"


def upsert_contact(conn, phone, name="", *, opt_out=None, sent_at=None):
    """Kisi kaydini gunceller; mesaj iliskileri send_history tablosunda tutulur."""
    phone = normalize_phone(phone)
    if not phone:
        return
    now = sent_at or datetime.now().isoformat(timespec="seconds")
    conn.execute("""INSERT INTO contacts(phone,name,opt_out,created_at,updated_at,last_sent_at)
        VALUES(?,?,COALESCE(?,0),?,?,?)
        ON CONFLICT(phone) DO UPDATE SET
          name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE contacts.name END,
          opt_out=CASE WHEN ? IS NULL THEN contacts.opt_out ELSE excluded.opt_out END,
          updated_at=CASE WHEN ? IS NULL THEN contacts.updated_at ELSE excluded.updated_at END,
          last_sent_at=CASE WHEN excluded.last_sent_at<>'' THEN excluded.last_sent_at ELSE contacts.last_sent_at END""",
        (phone, name or "", opt_out, now, now, sent_at or "", opt_out, opt_out))


def contact_is_opted_out(phone, conn=None):
    if conn is None:
        with history_db() as db:
            return contact_is_opted_out(phone, db)
    row = conn.execute("SELECT opt_out FROM contacts WHERE phone=?", (normalize_phone(phone),)).fetchone()
    return bool(row and row["opt_out"])


def ensure_saved_message(message, name="", message_id="", conn=None):
    message = (message or "").strip()
    if not message:
        return ""
    if conn is None:
        with history_db() as db:
            return ensure_saved_message(message, name, message_id, db)
    if message_id:
        row = conn.execute("SELECT id FROM saved_messages WHERE id=? AND message=?", (message_id, message)).fetchone()
        if row:
            return row["id"]
    row = conn.execute("SELECT id FROM saved_messages WHERE message=?", (message,)).fetchone()
    if row:
        return row["id"]
    message_id = uuid.uuid4().hex
    message_name = (name or "").strip() or f"Mesaj {datetime.now():%Y-%m-%d %H:%M}"
    conn.execute("INSERT OR IGNORE INTO saved_messages (id, name, message, created_at) VALUES (?, ?, ?, ?)",
                 (message_id, message_name, message, datetime.now().isoformat(timespec="seconds")))
    row = conn.execute("SELECT id FROM saved_messages WHERE message=?", (message,)).fetchone()
    return row["id"] if row else message_id


def load_history():
    global sent_message_map
    with history_db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS send_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '',
            sent_at TEXT NOT NULL, job_id TEXT NOT NULL DEFAULT '', job_name TEXT NOT NULL DEFAULT '',
            result TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', image TEXT NOT NULL DEFAULT '',
            message_key TEXT NOT NULL DEFAULT '', message_id TEXT NOT NULL DEFAULT '',
            template_id TEXT NOT NULL DEFAULT ''
        )""")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(send_history)")}
        if "message_key" not in columns:
            conn.execute("ALTER TABLE send_history ADD COLUMN message_key TEXT NOT NULL DEFAULT ''")
        if "message_id" not in columns:
            conn.execute("ALTER TABLE send_history ADD COLUMN message_id TEXT NOT NULL DEFAULT ''")
        if "template_id" not in columns:
            conn.execute("ALTER TABLE send_history ADD COLUMN template_id TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE TABLE IF NOT EXISTS saved_messages (id TEXT PRIMARY KEY, name TEXT NOT NULL, message TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)")
        conn.execute("""CREATE TABLE IF NOT EXISTS contacts (
            phone TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', opt_out INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_sent_at TEXT NOT NULL DEFAULT ''
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS message_templates (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, message TEXT NOT NULL,
            message_id TEXT NOT NULL DEFAULT '', image TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        template_columns = {row["name"] for row in conn.execute("PRAGMA table_info(message_templates)")}
        if "image" not in template_columns:
            conn.execute("ALTER TABLE message_templates ADD COLUMN image TEXT NOT NULL DEFAULT ''")
        conn.execute("""CREATE TABLE IF NOT EXISTS sent_registry (
            phone TEXT NOT NULL, message_id TEXT NOT NULL, status TEXT NOT NULL,
            sent_at TEXT NOT NULL, job_id TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (phone, message_id)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_send_history_phone_message ON send_history(phone, message_key, result)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_send_history_phone_result_message ON send_history(phone, result, message_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_send_history_result_time_phone ON send_history(result, sent_at, phone)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_send_history_template_result_phone ON send_history(template_id, result, phone)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_send_history_time ON send_history(sent_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS app_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL, level TEXT NOT NULL DEFAULT 'INFO',
            event TEXT NOT NULL DEFAULT 'runtime', job_id TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '', message TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_app_logs_created_at ON app_logs(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_app_logs_job_id_id ON app_logs(job_id, id)")
        global _log_table_ready
        _log_table_ready = True
        for template in conn.execute("SELECT id,name,message FROM message_templates WHERE message_id='' ").fetchall():
            stable_id = ensure_saved_message(template["message"], template["name"], conn=conn)
            conn.execute("UPDATE message_templates SET message_id=? WHERE id=?", (stable_id, template["id"]))
        # Sablon eski surumlerde duzenlenince yeni message_id aliyordu. Sablon ID'si
        # sabit oldugundan o gonderimleri mevcut kalici kimlige tasiyip yeniden gonderimi engelle.
        conn.execute("""UPDATE send_history
            SET message_id=(SELECT t.message_id FROM message_templates t WHERE t.id=send_history.template_id)
            WHERE template_id<>'' AND EXISTS(SELECT 1 FROM message_templates t WHERE t.id=send_history.template_id)
              AND message_id<>(SELECT t.message_id FROM message_templates t WHERE t.id=send_history.template_id)""")
        jobs_changed = False
        for job in jobs:
            for recipient in job.get("recipients", []):
                if not recipient.get("message_id"):
                    template = (recipient.get("message") or job.get("message") or "").strip()
                    if template:
                        recipient["message_id"] = ensure_saved_message(
                            template, job.get("name", ""), job.get("message_id", ""), conn
                        )
                        jobs_changed = True
        old_jobs = {j.get("id"): j for j in jobs}
        old_recipients = {
            job.get("id"): {r.get("phone"): r for r in job.get("recipients", [])}
            for job in jobs
        }
        # Onceki surumde tekrar gonderme secenegi farkli bir message_id olusturuyordu.
        # Bu gorevlerin gecmisini asil mesaj kimligine baglayarak tekrar gonderimi engelle.
        for job in jobs:
            if not job.get("allow_resend"):
                continue
            for recipient in job.get("recipients", []):
                original_id = recipient.get("queue_message_id")
                if original_id:
                    conn.execute("UPDATE send_history SET message_id=? WHERE job_id=? AND phone=?",
                                 (original_id, job.get("id", ""), recipient.get("phone", "")))
        for row in conn.execute("SELECT id, phone, message, image, job_id FROM send_history WHERE message_key='' ").fetchall():
            old_job = old_jobs.get(row["job_id"], {})
            old_rec = old_recipients.get(row["job_id"], {}).get(row["phone"], {})
            template = (old_rec.get("message") or old_job.get("message") or row["message"]).strip()
            image = row["image"] or old_job.get("image", "")
            message_id = ensure_saved_message(template, old_job.get("name", ""), old_job.get("message_id", ""), conn)
            conn.execute("UPDATE send_history SET message_key=?, message_id=? WHERE id=?",
                         (message_fingerprint(template, image), message_id, row["id"]))
        # Onceki surumlerde fingerprint kaydedilmis ama mesaj ID'si bos kalmis olabilir.
        for row in conn.execute("SELECT id, phone, message, image, job_id FROM send_history WHERE message_id='' ").fetchall():
            old_job = old_jobs.get(row["job_id"], {})
            old_rec = old_recipients.get(row["job_id"], {}).get(row["phone"], {})
            template = (old_rec.get("message") or old_job.get("message") or row["message"]).strip()
            message_id = ensure_saved_message(template, old_job.get("name", ""), old_job.get("message_id", ""), conn)
            if message_id:
                conn.execute("UPDATE send_history SET message_id=? WHERE id=?", (message_id, row["id"]))
        known = {(row[0], row[1]) for row in conn.execute(
            "SELECT DISTINCT phone, message_key FROM send_history WHERE result IN ('ok', 'belirsiz')")}
        # Eski CSV gorevlerinde alici bazinda basarili durum tutuluyordu; bunlari gecmise aktar.
        for job in jobs:
            for r in job.get("recipients", []):
                phone = r.get("phone")
                template = (r.get("message") or job.get("message") or "").strip()
                image = job.get("image") or ""
                key = message_fingerprint(template, image)
                if r.get("status") != "gonderildi" or not phone or (phone, key) in known:
                    continue
                message = template.replace("{isim}", r.get("name") or "")
                message_id = job.get("message_id", "")
                conn.execute("""INSERT INTO send_history
                    (phone, name, message, sent_at, job_id, job_name, result, image, message_key, message_id)
                    VALUES (?, ?, ?, ?, ?, ?, 'ok', ?, ?, ?)""",
                    (phone, r.get("name", ""), message, r.get("time") or datetime.now().isoformat(timespec="seconds"),
                     job.get("id", ""), job.get("name", ""), image, key, message_id))
                known.add((phone, key))
        # Kalici gonderim kilidi: ayni kayitli mesaj ID'si ayni telefona bir kez islenir.
        conn.execute("""INSERT OR IGNORE INTO sent_registry
            (phone, message_id, status, sent_at, job_id, error)
            SELECT phone, CASE WHEN message_id='' THEN 'legacy:' || message_key ELSE message_id END,
                   result, sent_at, job_id, error
            FROM send_history WHERE result IN ('ok', 'belirsiz')""")
        # Eski gonderim gecmislerini merkezi kisi listesine aktar.
        for row in conn.execute("""SELECT phone, name, MAX(sent_at) AS sent_at FROM send_history
                                  WHERE result='ok' GROUP BY phone""").fetchall():
            upsert_contact(conn, row["phone"], row["name"], sent_at=row["sent_at"])
        rows = conn.execute("SELECT * FROM send_history WHERE result IN ('ok', 'belirsiz') ORDER BY id").fetchall()
        sent_message_map = {
            (row["phone"], message_identity(row["message_id"], row["message_key"])): dict(row)
            for row in rows
        }
        if jobs_changed:
            save_jobs()


def phone_blocks(recipients, message="", image="", exclude_job_id=None):
    """lock icinde cagrilir. Gecmis veya baska aktif gorevde bulunan numaralari ayirir."""
    queued = set()
    for job in jobs:
        if job.get("id") == exclude_job_id or job.get("status") not in ("bekliyor", "calisiyor", "duraklatildi"):
            continue
        if job.get("type") == "liste":
            queued.update((r.get("phone"), message_identity(
                              r.get("queue_message_id") or r.get("message_id") or job.get("message_id", ""),
                              message_fingerprint(r.get("message") or job.get("message", ""), job.get("image", ""))))
                          for r in job.get("recipients", []) if r.get("status") == "bekliyor")
        else:
            start = job.get("recipient_index", 0) if job.get("status") == "calisiyor" else 0
            queued.update((r.get("phone"), message_identity(
                              r.get("queue_message_id") or r.get("message_id") or job.get("message_id", ""),
                              message_fingerprint(r.get("message") or job.get("message", ""), job.get("image", ""))))
                          for r in job.get("recipients", [])[start:])

    with history_db() as conn:
        opted_out = {row["phone"] for row in conn.execute("SELECT phone FROM contacts WHERE opt_out=1")}
    accepted, skipped, seen = [], [], set()
    for r in recipients:
        phone = r.get("phone", "")
        template = (r.get("message") or message or "").strip()
        message_key = message_fingerprint(template, image)
        history_pair = (phone, message_identity(r.get("queue_message_id") or r.get("message_id"), message_key))
        if phone in opted_out:
            skipped.append({"row": r.get("row", "-"), "name": r.get("name", ""), "raw": phone,
                            "reason": "kisi mesaj almak istemiyor", "src": r.get("src")})
        elif history_pair in seen:
            skipped.append({"row": r.get("row", "-"), "name": r.get("name", ""), "raw": phone,
                            "reason": "ayni yuklemede tekrar", "src": r.get("src")})
        elif history_pair in sent_message_map:
            prev = sent_message_map[history_pair]
            when = prev.get("sent_at", "")
            skipped.append({"row": r.get("row", "-"), "name": r.get("name", ""), "raw": phone,
                            "reason": f"bu mesaj daha once gonderildi{f' ({when})' if when else ''}", "src": r.get("src")})
        elif history_pair in queued:
            skipped.append({"row": r.get("row", "-"), "name": r.get("name", ""), "raw": phone,
                            "reason": "bu mesaj baska bir aktif gorevde sirada", "src": r.get("src")})
        else:
            accepted.append(r)
            seen.add(history_pair)
    return accepted, skipped


def record_attempt(job, recipient, message, result, note="", message_key="", message_id="", template_id=""):
    """Basarili/hatali gonderimi telefon ve tam mesajla kalici gecmise yazar (lock icinde)."""
    item = {
        "phone": recipient.get("phone", ""), "name": recipient.get("name", ""),
        "message": message, "sent_at": datetime.now().isoformat(timespec="seconds"),
        "job_id": job.get("id", ""), "job_name": job.get("name", ""),
        "result": result, "error": note, "image": job.get("image") or "",
        "message_key": message_key, "message_id": message_id,
        "template_id": template_id or recipient.get("template_id") or job.get("template_id", ""),
    }
    with history_db() as conn:
        cursor = conn.execute("""INSERT INTO send_history
            (phone, name, message, sent_at, job_id, job_name, result, error, image, message_key, message_id, template_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (item["phone"], item["name"], item["message"], item["sent_at"], item["job_id"],
             item["job_name"], item["result"], item["error"], item["image"], item["message_key"], item["message_id"], item["template_id"]))
        if result == "ok":
            upsert_contact(conn, item["phone"], item["name"], sent_at=item["sent_at"])
        item["id"] = cursor.lastrowid
        conn.execute("""INSERT INTO sent_registry
            (phone, message_id, status, sent_at, job_id, error) VALUES (?, ?, ?, ?, ?, ?)""",
            (item["phone"], message_identity(message_id, message_key), result,
             item["sent_at"], item["job_id"], note))
    if result in ("ok", "belirsiz"):
        sent_message_map[(item["phone"], message_identity(message_id, message_key))] = item
    return item["id"]


def backfill_template_history(conn):
    """Eski kayitlarda bos kalan template_id alanini tekil message_id eslesmesiyle tamamla."""
    conn.execute("""UPDATE send_history
        SET template_id=(SELECT t.id FROM message_templates t
                         WHERE t.message_id=send_history.message_id)
        WHERE template_id='' AND message_id<>''
          AND 1=(SELECT COUNT(*) FROM message_templates t
                 WHERE t.message_id=send_history.message_id)""")


def finish_attempt(attempt_id, result, note=""):
    """Onceden yazilan gonderim rezervasyonunu sonucuyla kapatir."""
    with history_db() as conn:
        conn.execute("UPDATE send_history SET result=?, error=? WHERE id=?",
                     (result, note, attempt_id))
        row = conn.execute("SELECT * FROM send_history WHERE id=?", (attempt_id,)).fetchone()
        if row:
            registry_id = message_identity(row["message_id"], row["message_key"])
            if result in ("ok", "belirsiz"):
                conn.execute(
                    "UPDATE sent_registry SET status=?, error=? WHERE phone=? AND message_id=? AND job_id=?",
                    (result, note, row["phone"], registry_id, row["job_id"]),
                )
                if result == "ok":
                    upsert_contact(conn, row["phone"], row["name"], sent_at=row["sent_at"])
            else:
                conn.execute(
                    "DELETE FROM sent_registry WHERE phone=? AND message_id=? AND job_id=?",
                    (row["phone"], registry_id, row["job_id"]),
                )
    if not row:
        return
    pair = (row["phone"], message_identity(row["message_id"], row["message_key"]))
    if result in ("ok", "belirsiz"):
        sent_message_map[pair] = dict(row)
    elif sent_message_map.get(pair, {}).get("id") == attempt_id:
        sent_message_map.pop(pair, None)


def previously_sent(phone, message_id, message_key):
    return (phone, message_identity(message_id, message_key)) in sent_message_map


def load_jobs():
    global jobs
    backup = JOBS_FILE.with_name(JOBS_FILE.name + ".bak")
    loaded_from_backup = False
    errors = []
    for source in (JOBS_FILE, backup):
        if not source.exists():
            continue
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("Gorev dosyasinin ana degeri liste degil.")
            jobs = data
            loaded_from_backup = source == backup
            break
        except (OSError, ValueError, json.JSONDecodeError) as e:
            errors.append(f"{source.name}: {e}")
    else:
        jobs = []
        if errors:
            raise RuntimeError("Gorev dosyasi ve yedegi okunamadi: " + "; ".join(errors))

    changed = loaded_from_backup
    for j in jobs:
        for recipient in j.get("recipients", []):
            if recipient.get("status") == "hata" and whatsapp_number_unavailable(recipient.get("error", "")):
                recipient["status"] = "gecersiz"
                recipient["error"] = "WhatsApp bu numaranin hesabinda kayitli olmadigini bildirdi; tekrar denenmedi."
                log("Onceki hata kaydi WhatsApp'ta kayitli olmayan numara olarak duzeltildi.",
                    level="WARNING", event="recipient_skipped", job_id=j.get("id"),
                    phone=recipient.get("phone", ""))
                changed = True
        if j.get("status") == "calisiyor":
            current = j.get("current_recipient") or {}
            next_name = current.get("name") or ""
            next_phone = current.get("phone") or ""
            position = f"{next_name} ({next_phone})" if next_name else next_phone
            j["status"] = "bekliyor"
            j["next_run"] = datetime.now().isoformat(timespec="seconds")
            j["resume_notice"] = (f"Uygulama yeniden baslatildi; liste kaldigi yerden devam edecek."
                                  + (f" Son islemdeki kisi: {position}." if position else ""))
            changed = True
    if loaded_from_backup:
        log("Gorev dosyasi bozuktu; son yedekten gorevler kurtarildi.")
    if changed:
        save_jobs()


def find_job(job_id):
    """lock icinde cagrilmali"""
    return next((j for j in jobs if j["id"] == job_id), None)


def wake(job):
    """Tamamlanmis bir gorevde yeniden is cikarsa gorevi tekrar siraya alir (lock icinde cagrilir)."""
    if job["status"] == "tamamlandi":
        job["status"] = "bekliyor"
        job["next_run"] = datetime.now().isoformat(timespec="seconds")
        job["note"] = ""


def clean_image_name(name):
    """Panelden gelen resim adini dogrular; gecerli ve dosya varsa adi, yoksa '' dondurur."""
    name = (name or "").strip()
    if name and IMAGE_RE.match(name) and (IMAGES_DIR / name).exists():
        return name
    return ""


def job_image(job):
    """Gorevin resim dosyasinin yolunu dondurur (resim yoksa None; dosya kaybolduysa hata verir)."""
    name = job.get("image") or ""
    if not name:
        return None
    path = IMAGES_DIR / name
    if not IMAGE_RE.match(name) or not path.exists():
        raise FileNotFoundError(f"Resim dosyasi bulunamadi: {name}")
    return path


# ---------------------------------------------------------------- worker

def wait_for_link_preview(page, message, max_wait=15):
    """Mesajda link varsa, onizleme kutusu cikana kadar (en fazla max_wait sn) bekler."""
    if "http" not in message and "www." not in message:
        return
    try:
        footer = page.query_selector("footer")
        base = footer.bounding_box()["height"]
    except Exception:
        time.sleep(max_wait)
        return
    start = time.time()
    while time.time() - start < max_wait:
        time.sleep(0.5)
        try:
            h = footer.bounding_box()["height"]
        except Exception:
            continue
        if h > base + 20:  # onizleme kutusu eklenince alt alan buyur
            time.sleep(1.5)
            return
    # onizleme algilanamadiysa da max_wait kadar beklenmis olur, devam edilir


def snap(page, phone, tag):
    """Hata aninda ekran goruntusu kaydeder (hata_ekran klasorune)."""
    try:
        SHOT_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(SHOT_DIR / f"{phone}_{tag}_{datetime.now():%H%M%S}.png"))
    except Exception:
        pass


def visible_dialog_text(page):
    """Gorunen WhatsApp uyari penceresinin metnini alir; gecici pencereleri zorla kapatmaz."""
    try:
        dialogs = page.locator(DIALOG_SELECTOR)
        for i in range(dialogs.count()):
            dialog = dialogs.nth(i)
            if dialog.is_visible():
                text = " ".join((dialog.inner_text() or "").split())
                if text:
                    return text[:160]
    except Exception:
        pass
    return ""


def whatsapp_number_unavailable(text):
    """WhatsApp'in numaranin hesapta olmadigini bildiren uyarisini tani."""
    low = " ".join((text or "").lower().replace("’", "'").split())
    phrases = (
        "not on whatsapp", "isn't on whatsapp", "is not on whatsapp",
        "isnt on whatsapp", "not registered on whatsapp", "isn't registered on whatsapp",
        "doesn't have whatsapp", "does not have whatsapp", "not a whatsapp user",
        "whatsapp'ta kayitli degil", "whatsapp'ta kayıtlı değil",
        "whatsapp'a kayitli degil", "whatsapp'a kayıtlı değil",
        "whatsapp kullanmiyor", "whatsapp kullanmıyor", "whatsapp'ta degil",
        "whatsapp'ta değil",
    )
    return any(phrase in low for phrase in phrases)


def send_image(page, phone, caption, image_path, job_id="", job_name=""):
    """Resmi sohbete yapistirir (Ctrl+V gibi), yazi varsa baslik olarak ekler ve gonderir."""
    log(f"{job_name}: Resim dosyasi WhatsApp'a hazirlaniyor.", event="send_step", job_id=job_id, phone=phone)
    image_path = Path(image_path)
    try:
        mime = mimetypes.guess_type(str(image_path))[0] or "image/png"
        b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    except OSError as e:
        log(f"Resim dosyasi okunamadi: {e}", level="ERROR", event="send_step", job_id=job_id, phone=phone)
        return "hata", f"Resim dosyasi okunamadi: {e}"
    try:
        page.evaluate(PASTE_JS, {"sel": INPUT_SELECTOR, "b64": b64, "mime": mime, "name": image_path.name})
    except Exception as e:
        log(f"Resim panoya aktarilamadi: {e}", level="ERROR", event="send_step", job_id=job_id, phone=phone)
        return "hata", f"Resim gonderime hazirlanamadi: {e}"
    log(f"{job_name}: WhatsApp resim onizlemesi bekleniyor.", event="send_step", job_id=job_id, phone=phone)
    try:
        page.wait_for_selector(PREVIEW_SEND_SELECTOR, timeout=15000)
    except Exception:
        snap(page, phone, "onizleme_yok")
        log("WhatsApp resim onizlemesi gorunmedi.", level="WARNING", event="send_step", job_id=job_id, phone=phone)
        return "hata", "Resim onizlemesi acilmadi (WhatsApp arayuzu degismis olabilir; hata_ekran klasorundeki goruntuye bak)"
    time.sleep(1.0)
    if caption:
        log(f"{job_name}: Resim aciklamasi ekleniyor.", event="send_step", job_id=job_id, phone=phone)
        for i, line in enumerate(caption.split("\n")):
            if i:
                page.keyboard.press("Shift+Enter")
            if line:
                page.keyboard.insert_text(line)
        time.sleep(0.5)
    try:
        # Enter gonderimi baslatabilir; bu noktadan sonraki her hata belirsizdir.
        log(f"{job_name}: Resim icin gonder tusu uygulaniyor; onizlemenin kapanmasi bekleniyor.",
            event="send_step", job_id=job_id, phone=phone)
        page.keyboard.press("Enter")
        # gonderilince onizleme kapanir ve gonder dugmesi kaybolur
        page.wait_for_selector(PREVIEW_SEND_SELECTOR, state="detached", timeout=20000)
    except Exception:
        snap(page, phone, "gonderim_belirsiz")
        log("Resim gonderiminden sonra onizleme kapanmadi; sonuc belirsiz.", level="WARNING",
            event="send_step", job_id=job_id, phone=phone)
        return "belirsiz", "Gonderim dogrulanamadi (resim gitmis olabilir, WhatsApp'tan kontrol et)"
    time.sleep(2.0)
    log(f"{job_name}: Resim gonderimi WhatsApp tarafinda dogrulandi.", event="send_step", job_id=job_id, phone=phone)
    return "ok", ""


def send_message(page, phone, message, image=None, job_id="", job_name=""):
    """(durum, aciklama) dondurur. durum: 'ok' | 'gecersiz' | 'hata' | 'belirsiz'.
    image: gonderilecek resmin yolu (varsa mesaj metni resmin basligi olur).
    'belirsiz': gonderim dogrulanamadi, mesaj gitmis olabilir (otomatik tekrar denenmez).
    'gecersiz': WhatsApp numarayi kabul etmedi (kayitli degil / hatali numara).
    'hata': gecici sorun olabilir (internet, sayfa yavasligi, beklenmeyen pencere)."""
    # Resimli gonderimde metin URL ile degil, resmin basligi olarak yazilir
    url = f"{WA_URL}/send?phone={phone}" + ("" if image else f"&text={quote(message)}")
    log(f"{job_name}: WhatsApp sohbeti aciliyor.", event="send_step", job_id=job_id, phone=phone)
    try:
        page.goto(url)
    except Exception as e:
        log(f"Sohbet sayfasi acilamadi: {e}", level="ERROR", event="send_step", job_id=job_id, phone=phone)
        return "hata", f"Sohbet acilamadi; mesaj gonderme islemi baslamadi: {e}"
    try:
        # WhatsApp sohbet acilirken gecici "Starting chat / Cancel" penceresi gosterebilir.
        # Bu pencereyi sonuc kabul etme; gercek mesaj kutusu gorunur olana kadar bekle.
        page.wait_for_selector(INPUT_SELECTOR, state="visible", timeout=45000)
        log(f"{job_name}: Sohbet hazir, mesaj kutusu bulundu.", event="send_step", job_id=job_id, phone=phone)
    except Exception:
        txt = visible_dialog_text(page)
        low = txt.lower()
        if whatsapp_number_unavailable(txt) or any(
                k in low for k in ("invalid", "gecersiz", "geçersiz")):
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            log(f"WhatsApp bu numaranin hesabinda kayitli olmadigini bildirdi; otomatik tekrar yapilmayacak: {txt}", level="WARNING",
                event="send_step", job_id=job_id, phone=phone)
            return "gecersiz", txt or "WhatsApp bu numaranin hesabinda kayitli olmadigini bildirdi"
        detail = f" ({txt})" if txt else ""
        log(f"Sohbet 45 saniyede hazir olmadi{detail}.", level="WARNING",
            event="send_step", job_id=job_id, phone=phone)
        return "hata", f"Sohbet 45 sn icinde hazir olmadi{detail}; gecici ag veya WhatsApp sorunu olabilir"
    if image:
        return send_image(page, phone, message, image, job_id, job_name)
    time.sleep(1.5)
    log(f"{job_name}: Metin mesajinin gonderime hazir oldugu kontrol ediliyor.",
        event="send_step", job_id=job_id, phone=phone)
    wait_for_link_preview(page, message)
    log(f"{job_name}: Mesaj onizleme kontrolu tamamlandi.", event="send_step", job_id=job_id, phone=phone)
    try:
        before = page.locator("div.message-out").all_inner_texts()
    except Exception:
        before = []
    normalize = lambda value: " ".join((value or "").split())
    target = normalize(message)
    before_matches = sum(1 for value in before if target and target in normalize(value))
    before_out_count = len(before)
    try:
        draft_before = normalize(page.locator(INPUT_SELECTOR).last.inner_text())
    except Exception:
        draft_before = ""
    try:
        # Tek bir gonderim eylemi uygula. Sonuc kesin degilse tekrar tiklama;
        # ayni mesajin iki kez gitme riski olusmasin.
        log(f"{job_name}: Gonder tusu uygulaniyor; WhatsApp giden mesaj dogrulamasi bekleniyor.",
            event="send_step", job_id=job_id, phone=phone)
        page.locator(INPUT_SELECTOR).last.press("Enter")
        page.wait_for_function(
            """({text, matchCount, outCount, draft}) => {
                const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
                const outgoing = [...document.querySelectorAll('div.message-out')];
                const found = outgoing.filter(e => norm(e.innerText).includes(norm(text))).length;
                const composers = [...document.querySelectorAll('footer div[contenteditable="true"]')]
                    .filter(e => e.getClientRects().length);
                const composer = composers[composers.length - 1];
                const cleared = !!draft && !!composer && !norm(composer.innerText);
                return found > matchCount || outgoing.length > outCount || cleared;
            }""",
            arg={"text": message, "matchCount": before_matches, "outCount": before_out_count, "draft": draft_before},
            timeout=12000,
        )
    except Exception as e:
        snap(page, phone, "gonderim_dogrulanamadi")
        log(f"Mesaj gonderimi kesin olarak dogrulanamadi: {e}", level="WARNING",
            event="send_step", job_id=job_id, phone=phone)
        return "belirsiz", f"Gonderim sonucu dogrulanamadi; tekrar gonderim engellendi: {e}"
    log(f"{job_name}: Mesaj gonderimi WhatsApp tarafinda dogrulandi.",
        event="send_step", job_id=job_id, phone=phone)
    return "ok", ""


def job_exists(job_id):
    with lock:
        return any(j["id"] == job_id for j in jobs)


def run_job(page, job):
    """Tur bazli gorev: listedeki herkese mesaj atar, sonraki tur icin zamanlar."""
    log(f"Gonderim turu basladi: {job['name']} ({len(job['recipients'])} kisi)", event="send_step", job_id=job.get("id"))
    start_index = job.get("recipient_index", 0)
    for i in range(start_index, len(job["recipients"])):
        with lock:
            if job not in jobs:
                log("Gorev iptal edildi.")
                return
            if job["status"] == "duraklatildi":
                save_jobs()
                log("Gorev duraklatildi.")
                return
            r = job["recipients"][i]
            log(f"Alici {i + 1}/{len(job['recipients'])} kontrol ediliyor.", event="recipient_selected",
                job_id=job.get("id"), phone=r.get("phone", ""))
            template = (r.get("message") or job["message"] or "").strip()
            message_key = message_fingerprint(template, job.get("image", ""))
            message_id = (r.get("queue_message_id") if job.get("allow_resend") else "") or r.get("message_id") or job.get("message_id", "")
            if previously_sent(r["phone"], message_id, message_key):
                log("Ayni mesaj daha once gonderildigi icin alici atlandi.", event="recipient_skipped",
                    job_id=job.get("id"), phone=r["phone"])
                job.setdefault("skipped", []).append({
                    "row": "-", "name": r.get("name", ""), "raw": r["phone"],
                    "reason": "bu mesaj daha once gonderildi", "src": r.get("src"),
                })
                job["recipient_index"] = i + 1
                save_jobs()
                continue
            if contact_is_opted_out(r["phone"]):
                log("Kisi mesaj istemiyor listesinde oldugu icin alici atlandi.", event="recipient_skipped",
                    job_id=job.get("id"), phone=r["phone"])
                job.setdefault("skipped", []).append({
                    "row": "-", "name": r.get("name", ""), "raw": r["phone"],
                    "reason": "kisi mesaj almak istemiyor", "src": r.get("src"),
                })
                job["recipient_index"] = i + 1
                save_jobs()
                continue
        text = template.replace("{isim}", r.get("name") or "")
        ok = False
        # Once gonderim baslamadan once kaydi diske yaz. Uygulama tam bu
        # sirada kapanirsa yeniden baslatmada ayni aliciya tekrar gitmesin.
        with lock:
            attempt_id = record_attempt(
                job, r, text, "belirsiz",
                "Gonderim baslatildi; sonuc kesinlesmedi.", message_key, message_id,
                r.get("template_id") or job.get("template_id", ""),
            )
            log("Gonderim denemesi kaydedildi; WhatsApp adimlari baslatiliyor.", event="send_step",
                job_id=job.get("id"), phone=r["phone"])
        try:
            st, note = send_message(page, r["phone"], text, job_image(job), job.get("id", ""), job.get("name", ""))
            ok = st == "ok"
            if not ok:
                log(f"{r['phone']}: {note}", level="WARNING", event="send_result", job_id=job.get("id"), phone=r["phone"])
        except Exception as e:
            # send_message disindan gelen hata, gonder tusuna basilip basilmadigini
            # garanti edemez. Gonderilmis olma ihtimalinde otomatik tekrar etme.
            st, note = "belirsiz", f"Gonderim sonucu belirsiz; otomatik tekrar engellendi: {e}"
            log(f"Belirsiz sonuc ({r['phone']}): {e}", level="ERROR", event="send_uncertain", job_id=job.get("id"), phone=r["phone"])
        with lock:
            finish_attempt(attempt_id, st, note)
            if job not in jobs:
                log("Gorev iptal edildi.")
                return
            job["recipient_index"] = i + 1
            if ok:
                job["sent"] += 1
            else:
                job["failed"] += 1
            save_jobs()
        log(f"{'Gonderildi' if ok else 'BASARISIZ'}: {r['phone']}", level="INFO" if ok else "WARNING",
            event="send_result", job_id=job.get("id"), phone=r["phone"])
        with lock:
            paused = job["status"] == "duraklatildi"
        if paused:
            return
        if i < len(job["recipients"]) - 1:
            delay = random.randint(job["min_delay"], job["max_delay"])
            log(f"Siradaki aliciya gecmeden once {delay} saniye bekleniyor.",
                event="job_wait", job_id=job.get("id"))
            time.sleep(delay)

    with lock:
        job["remaining"] -= 1
        if job["remaining"] <= 0:
            job["status"] = "tamamlandi"
            job["next_run"] = ""
        else:
            job["recipient_index"] = 0
            if job["status"] != "duraklatildi":
                job["status"] = "bekliyor"
            nxt = datetime.now() + timedelta(minutes=job["interval_min"])
            job["next_run"] = nxt.isoformat(timespec="seconds")
        save_jobs()
    log(f"Gorev turu bitti: {job['name']} (kalan tur: {max(job['remaining'], 0)})")


def next_window_start(job, now, force_tomorrow=False):
    start_today = now.replace(hour=job["win_start"], minute=0, second=0, microsecond=0)
    if not force_tomorrow and now < start_today:
        return start_today
    return start_today + timedelta(days=1)


def run_list_step(page, job):
    """CSV listesi gorevi: her cagrida EN FAZLA BIR kisiye mesaj atar.
    Gunluk limit ve saat araligina uyar, mesajlari gune yayar."""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    retrying_transient_error = False
    with lock:
        if job.get("day") != today:
            job["day"] = today
            job["sent_today"] = 0
        pending = [r for r in job["recipients"] if r["status"] == "bekliyor"]
        if not pending:
            job["status"] = "tamamlandi"
            job["next_run"] = ""
            job["note"] = ""
            save_jobs()
            log(f"Liste tamamlandi: {job['name']}")
            return
        if not (job["win_start"] <= now.hour < job["win_end"]):
            nxt = next_window_start(job, now)
            job["status"] = "bekliyor"
            job["next_run"] = nxt.isoformat(timespec="seconds")
            job["note"] = f"Saat araligi disinda, {nxt:%d.%m %H:%M} tarihinde devam edecek"
            save_jobs()
            log(f"Gonderim saati disinda; liste {nxt:%d.%m %H:%M} zamanina ertelendi.",
                event="job_wait", job_id=job.get("id"))
            return
        if job["sent_today"] >= job["daily_limit"]:
            nxt = next_window_start(job, now, force_tomorrow=True)
            job["status"] = "bekliyor"
            job["next_run"] = nxt.isoformat(timespec="seconds")
            job["note"] = f"Gunluk limit doldu, {nxt:%d.%m %H:%M} tarihinde devam edecek"
            save_jobs()
            log(f"{job['name']}: gunluk limit doldu ({job['daily_limit']}).")
            return
        r = pending[0]
        phone = r["phone"]
        log("Siradaki alici secildi; mesaj ve izin kaydi kontrol ediliyor.",
            event="recipient_selected", job_id=job.get("id"), phone=phone)
        job["current_recipient"] = {
            "row": r.get("row", "-"), "phone": phone, "name": r.get("name", ""),
            "started_at": now.isoformat(timespec="seconds"),
        }
        job["checkpoint_updated_at"] = now.isoformat(timespec="seconds")

        template = (r.get("message") or job["message"] or "").strip()
        message_key = message_fingerprint(template, job.get("image", ""))
        message_id = (r.get("queue_message_id") if job.get("allow_resend") else "") or r.get("message_id") or job.get("message_id", "")
        if previously_sent(phone, message_id, message_key):
            log("Ayni mesaj daha once gonderildigi icin alici atlandi.", event="recipient_skipped",
                job_id=job.get("id"), phone=phone)
            r["status"] = "daha_once_gonderildi"
            r["error"] = "Ayni mesaj bu numaraya daha once gonderilmis."
            r["time"] = datetime.now().isoformat(timespec="seconds")
            job["last_processed"] = {
                "row": r.get("row", "-"), "phone": phone, "name": r.get("name", ""),
                "status": r["status"], "time": r["time"],
            }
            job["current_recipient"] = None
            job["resume_notice"] = ""
            job.setdefault("skipped", []).append({
                "row": r.get("row", "-"), "name": r.get("name", ""), "raw": phone,
                "reason": "bu mesaj daha once gonderildi", "src": r.get("src"),
            })
            left = sum(1 for x in job["recipients"] if x["status"] == "bekliyor")
            if left == 0:
                job["status"] = "tamamlandi"
                job["next_run"] = ""
            else:
                job["next_run"] = (datetime.now() + timedelta(seconds=1)).isoformat(timespec="seconds")
            save_jobs()
            return
        if contact_is_opted_out(phone):
            log("Kisi mesaj istemiyor listesinde oldugu icin alici atlandi.", event="recipient_skipped",
                job_id=job.get("id"), phone=phone)
            r["status"] = "mesaj_istemiyor"
            r["error"] = "Kisi mesaj almak istemiyor olarak isaretlendi."
            r["time"] = datetime.now().isoformat(timespec="seconds")
            job["last_processed"] = {
                "row": r.get("row", "-"), "phone": phone, "name": r.get("name", ""),
                "status": r["status"], "time": r["time"],
            }
            job["current_recipient"] = None
            job["resume_notice"] = ""
            job.setdefault("skipped", []).append({
                "row": r.get("row", "-"), "name": r.get("name", ""), "raw": phone,
                "reason": "kisi mesaj almak istemiyor", "src": r.get("src"),
            })
            left = sum(1 for x in job["recipients"] if x["status"] == "bekliyor")
            if left == 0:
                job["status"] = "tamamlandi"
                job["next_run"] = ""
            else:
                job["next_run"] = (datetime.now() + timedelta(seconds=1)).isoformat(timespec="seconds")
            save_jobs()
            return

    text = template.replace("{isim}", r.get("name") or "")
    message_id = r.get("message_id") or job.get("message_id", "")
    with lock:
        # Gonderim baslamadan once tam devam noktasini JSON'a yaz.
        save_jobs()
        attempt_id = record_attempt(
            job, r, text, "belirsiz",
            "Gonderim baslatildi; sonuc kesinlesmedi.", message_key, message_id,
            r.get("template_id") or job.get("template_id", ""),
        )
        log("Gonderim denemesi kaydedildi; WhatsApp adimlari baslatiliyor.", event="send_step",
            job_id=job.get("id"), phone=phone)
    try:
        st, note = send_message(page, phone, text, job_image(job), job.get("id", ""), job.get("name", ""))
    except Exception as e:
        # Beklenmeyen Playwright hatasi gonder tusundan sonra olusmus olabilir.
        st, note = "belirsiz", f"Gonderim sonucu belirsiz; otomatik tekrar engellendi: {e}"
    ok = st == "ok"
    stamp = datetime.now().isoformat(timespec="seconds")

    with lock:
        finish_attempt(attempt_id, st, note)
        if job not in jobs:
            log("Gorev iptal edildi.")
            return
        job["last_processed"] = {
            "row": r.get("row", "-"), "phone": phone, "name": r.get("name", ""),
            "status": st, "time": stamp,
        }
        job["current_recipient"] = None
        job["checkpoint_updated_at"] = stamp
        job["resume_notice"] = ""
        if ok:
            r["status"] = "gonderildi"
            r["error"] = ""
            r["time"] = stamp
            job["sent"] += 1
            job["sent_today"] += 1
        elif st == "hata" and r.get("attempts", 0) < 1:
            # Gecici sohbet hatasi icin tek tekrar 20 saniye sonra yapilir.
            r["attempts"] = r.get("attempts", 0) + 1
            r["error"] = note + " (tekrar denenecek)"
            r["time"] = stamp
            retrying_transient_error = True
        else:
            r["status"] = "hata" if st == "belirsiz" else st  # 'gecersiz' veya 'hata'
            r["error"] = note
            r["time"] = stamp
            job["failed"] += 1
        left = sum(1 for x in job["recipients"] if x["status"] == "bekliyor")
        if left == 0:
            job["status"] = "tamamlandi"
            job["next_run"] = ""
            job["note"] = ""
        else:
            if retrying_transient_error:
                delay = 20
                log("Gecici sohbet hatasi; 2. deneme 20 saniye sonra baslatilacak.",
                    level="WARNING", event="job_wait", job_id=job.get("id"), phone=phone)
            elif job.get("spread"):
                span = (job["win_end"] - job["win_start"]) * 3600
                base = span / max(job["daily_limit"], 1)
                delay = max(20, random.uniform(0.7, 1.3) * base)
            else:
                delay = random.randint(job["min_delay"], job["max_delay"])
            if job["status"] != "duraklatildi":
                job["status"] = "bekliyor"
            job["next_run"] = (datetime.now() + timedelta(seconds=delay)).isoformat(timespec="seconds")
            job["note"] = ""
        save_jobs()
    label = "Gonderildi" if ok else f"BASARISIZ ({st}: {note})"
    log(f"{label}: {phone} ({job['name']}, kalan: {left})",
        level="INFO" if ok else "WARNING", event="send_result", job_id=job.get("id"), phone=phone)
    if left and job.get("next_run"):
        log(f"Sonraki alici adimi {job['next_run'].replace('T', ' ')} icin planlandi.",
            event="job_wait", job_id=job.get("id"))


def next_due():
    now = datetime.now()
    with lock:
        for j in jobs:
            if j["status"] == "bekliyor" and j["next_run"]:
                if datetime.fromisoformat(j["next_run"]) <= now:
                    j["status"] = "calisiyor"
                    log(f"Gorev baslatildi: {j['name']}.", event="job_started", job_id=j.get("id"))
                    return j
    return None


def launch_whatsapp(p):
    state["status"] = "WhatsApp tarayici baslatiliyor..."
    log("WhatsApp tarayici oturumu baslatiliyor.", event="whatsapp_start")
    ctx = p.chromium.launch_persistent_context(
        PROFILE_DIR,
        headless=False,
        viewport={"width": 1200, "height": 800},
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    log("WhatsApp Web aciliyor; hesap oturumu kontrol ediliyor.", event="whatsapp_connect")
    page.goto(WA_URL)
    state["status"] = "QR kodu okut / giris bekleniyor..."
    log("WhatsApp girisi bekleniyor; gerekirse QR kodunu okutun.", event="whatsapp_login")
    page.wait_for_selector(CHATLIST_SELECTOR, timeout=0)
    state["status"] = "Hazir - WhatsApp bagli"
    log("WhatsApp Web'e giris yapildi.")
    return ctx, page


def worker():
    with sync_playwright() as p:
        while True:
            ctx = None
            try:
                ctx, page = launch_whatsapp(p)
                while True:
                    # Kullanici WhatsApp penceresini kapatirsa ayni kapali
                    # sayfada isleri yeniden denemek yerine oturumu yenile.
                    if ctx.is_closed() or page.is_closed():
                        raise RuntimeError("WhatsApp tarayici penceresi kapandi")
                    job = next_due()
                    if job:
                        state["status"] = f"Gonderiliyor: {job['name']}"
                        try:
                            if job.get("type") == "liste":
                                run_list_step(page, job)
                            else:
                                run_job(page, job)
                        except Exception as e:
                            log(f"Beklenmeyen hata: {e}", level="ERROR", event="worker_error")
                            with lock:
                                if job["status"] == "calisiyor":
                                    job["status"] = "bekliyor"
                                    job["next_run"] = (datetime.now() + timedelta(minutes=2)).isoformat(timespec="seconds")
                                    save_jobs()
                        if ctx.is_closed() or page.is_closed():
                            raise RuntimeError("WhatsApp tarayici penceresi kapandi; baglanti yeniden kurulacak")
                        state["status"] = "Hazir - WhatsApp bagli"
                    else:
                        time.sleep(5)
            except Exception as e:
                log(f"WhatsApp baglantisi yenileniyor: {e}", level="WARNING", event="whatsapp_reconnect")
                state["status"] = "WhatsApp yeniden aciliyor..."
                if ctx:
                    try:
                        ctx.close()
                    except Exception:
                        pass
                time.sleep(3)


# ---------------------------------------------------------------- web

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify({"error": "Yuklenen dosya 25 MB sinirini asiyor."}), 413


@app.before_request
def reject_cross_origin_changes():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    origin = request.headers.get("Origin")
    allowed_origins = {"http://127.0.0.1:5001", "http://localhost:5001"}
    if origin and origin.rstrip("/") not in allowed_origins:
        return jsonify({"error": "Farkli bir kaynaktan degisiklik istegi reddedildi."}), 403
    return None


def summarize(j):
    s = {k: v for k, v in j.items() if k not in ("recipients", "skipped", "src_header")}
    n = len(j["recipients"])
    s["n_recipients"] = n
    s["n_skipped"] = len(j.get("skipped", []))
    if j.get("type") == "liste":
        cnt = {"bekliyor": 0, "gonderildi": 0, "gecersiz": 0, "hata": 0,
               "daha_once_gonderildi": 0, "mesaj_istemiyor": 0}
        for r in j["recipients"]:
            cnt[r["status"]] = cnt.get(r["status"], 0) + 1
        s["counts"] = cnt
        s["progress"] = f"{n - cnt['bekliyor']}/{n} (bugun {j.get('sent_today', 0)}/{j['daily_limit']})"
        s["next_recipient"] = next(({
            "row": r.get("row", "-"), "phone": r.get("phone", ""), "name": r.get("name", "")
        } for r in j["recipients"] if r.get("status") == "bekliyor"), None)
    else:
        s["progress"] = f"{max(j['remaining'], 0)}/{j['total']} tur kaldi"
    return s


@app.get("/")
def index():
    return Response(HTML, mimetype="text/html")


@app.get("/api/state")
def api_state():
    with lock:
        return jsonify({
            "status": state["status"],
            "log": state["log"][-40:],
            "jobs": [summarize(j) for j in jobs],
        })


@app.get("/api/status")
def api_status():
    with lock:
        return jsonify({"status": state["status"]})


@app.get("/api/dashboard/summary")
def api_dashboard_summary():
    today = datetime.now().date()
    first_day = today - timedelta(days=13)
    with history_db() as conn:
        contact_count = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        rows = conn.execute("""SELECT substr(sent_at,1,10) AS day, COUNT(DISTINCT phone) AS count
            FROM send_history WHERE result='ok' AND sent_at>=? AND sent_at<?
            GROUP BY substr(sent_at,1,10)""",
            (first_day.isoformat(), (today + timedelta(days=1)).isoformat())).fetchall()
    counts = {row["day"]: row["count"] for row in rows}
    days = []
    for offset in range(14):
        day = first_day + timedelta(days=offset)
        key = day.isoformat()
        days.append({"date": key, "label": day.strftime("%d.%m"), "count": counts.get(key, 0)})
    return jsonify({"contact_count": contact_count, "daily_sent": days})


def query_app_logs(query="", limit=1000):
    query = (query or "").strip().lower()
    where = ""
    params = ()
    if query:
        where = " WHERE lower(created_at || ' ' || level || ' ' || event || ' ' || job_id || ' ' || phone || ' ' || message) LIKE ?"
        params = (f"%{query}%",)
    with history_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM app_logs" + where, params).fetchone()[0]
        rows = conn.execute("SELECT * FROM app_logs" + where + " ORDER BY id DESC LIMIT ?",
                            params + (limit,)).fetchall()
    return total, [dict(row) for row in rows]


def stream_csv_export(filename, headers, sql, params=()):
    @stream_with_context
    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";")
        writer.writerow(headers)
        yield "\ufeff" + buffer.getvalue()
        with history_db() as conn:
            cursor = conn.execute(sql, params)
            while rows := cursor.fetchmany(1000):
                buffer.seek(0)
                buffer.truncate(0)
                writer.writerows(rows)
                yield buffer.getvalue()

    return Response(generate(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/logs")
def api_logs():
    try:
        limit = min(max(int(request.args.get("limit", "1000")), 1), 5000)
    except ValueError:
        limit = 1000
    total, items = query_app_logs(request.args.get("q", ""), limit)
    return jsonify({"total": total, "items": items})


@app.get("/api/logs/export")
def api_logs_export():
    query = (request.args.get("q", "") or "").strip().lower()
    where = ""
    params = ()
    if query:
        where = " WHERE lower(created_at || ' ' || level || ' ' || event || ' ' || job_id || ' ' || phone || ' ' || message) LIKE ?"
        params = (f"%{query}%",)
    return stream_csv_export("uygulama_loglari.csv",
        ["tarih", "seviye", "olay", "gorev_id", "telefon", "mesaj"],
        "SELECT created_at,level,event,job_id,phone,message FROM app_logs" + where + " ORDER BY id DESC", params)


@app.get("/api/history")
def api_history():
    query = request.args.get("q", "").strip().lower()
    try:
        limit = min(max(int(request.args.get("limit", "200")), 1), 2000)
    except ValueError:
        limit = 200
    with history_db() as conn:
        if query:
            pattern = f"%{query}%"
            where = " WHERE lower(phone || ' ' || name || ' ' || message || ' ' || message_id || ' ' || job_name || ' ' || result || ' ' || error) LIKE ?"
            params = (pattern,)
        else:
            where, params = "", ()
        total = conn.execute("SELECT COUNT(*) FROM send_history" + where, params).fetchone()[0]
        rows = conn.execute("SELECT * FROM send_history" + where + " ORDER BY id DESC LIMIT ?",
                            params + (limit,)).fetchall()
    return jsonify({"total": total, "items": [dict(row) for row in rows]})


@app.get("/api/history/export")
def api_history_export():
    return stream_csv_export("gonderim_gecmisi.csv",
        ["telefon", "isim", "mesaj", "tarih", "gorev", "mesaj_id", "sonuc", "aciklama"],
        "SELECT phone,name,message,sent_at,job_name,message_id,result,error FROM send_history ORDER BY id DESC")


@app.get("/api/contacts")
def api_contacts():
    query = request.args.get("q", "").strip().lower()
    try:
        limit = min(max(int(request.args.get("limit", "500")), 1), 2000)
    except ValueError:
        limit = 500
    where, params = (" WHERE lower(c.phone || ' ' || c.name) LIKE ?", (f"%{query}%",)) if query else ("", ())
    with history_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM contacts c" + where, params).fetchone()[0]
        rows = conn.execute("""SELECT c.phone,c.name,c.opt_out,c.created_at,c.updated_at,c.last_sent_at,
            (SELECT COUNT(DISTINCT CASE WHEN h.message_id<>'' THEN h.message_id ELSE 'legacy:' || h.message_key END) FROM send_history h
             WHERE h.phone=c.phone AND h.result='ok') AS message_count
            FROM contacts c""" + where + " ORDER BY c.opt_out DESC,c.last_sent_at DESC,c.phone LIMIT ?",
            params + (limit,)).fetchall()
    return jsonify({"total": total, "items": [dict(row) for row in rows]})


@app.get("/api/contacts/export")
def api_contacts_export():
    with history_db() as conn:
        rows = conn.execute("""SELECT c.phone,c.name,c.opt_out,c.created_at,c.updated_at,c.last_sent_at,
            (SELECT group_concat(tracked_id, ' | ') FROM
              (SELECT DISTINCT CASE WHEN message_id<>'' THEN message_id ELSE 'legacy:' || message_key END AS tracked_id
               FROM send_history h WHERE h.phone=c.phone AND h.result='ok' ORDER BY tracked_id)) AS message_ids
            FROM contacts c ORDER BY c.opt_out DESC,c.last_sent_at DESC,c.phone""").fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["telefon", "isim", "mesaj_istemiyor", "son_basarili_gonderim", "basarili_mesaj_idleri"])
    for row in rows:
        writer.writerow([row["phone"], row["name"], "evet" if row["opt_out"] else "hayir",
                         row["last_sent_at"], row["message_ids"] or ""])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="kisiler.csv"'})


@app.get("/api/contacts/<phone>/messages")
def api_contact_messages(phone):
    normalized = normalize_phone(phone)
    if not normalized:
        return jsonify({"error": "Telefon numarasi gecersiz."}), 400
    with history_db() as conn:
        rows = conn.execute("""SELECT CASE WHEN message_id<>'' THEN message_id ELSE 'legacy:' || message_key END AS message_id,
            message,job_name,sent_at,result,error FROM send_history
            WHERE phone=? ORDER BY id DESC LIMIT 500""", (normalized,)).fetchall()
    return jsonify({"phone": normalized, "items": [dict(row) for row in rows]})


@app.post("/api/contacts")
def api_add_contact_opt_out():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Gecersiz istek govdesi."}), 400
    phone = normalize_phone(data.get("phone", ""))
    if not phone:
        return jsonify({"error": "Gecerli bir telefon numarasi gerekli."}), 400
    with history_db() as conn:
        upsert_contact(conn, phone, (data.get("name") or "").strip(), opt_out=True)
        row = conn.execute("SELECT phone,name,opt_out,last_sent_at FROM contacts WHERE phone=?", (phone,)).fetchone()
    log(f"Mesaj istemiyor listesine eklendi: {phone}", event="contact_opt_out", phone=phone)
    return jsonify({"ok": True, "item": dict(row)})


@app.patch("/api/contacts/<phone>")
def api_update_contact(phone):
    normalized = normalize_phone(phone)
    data = request.get_json(silent=True) or {}
    if not normalized or not isinstance(data, dict) or not isinstance(data.get("opt_out"), bool):
        return jsonify({"error": "Telefon ve opt_out (true/false) gerekli."}), 400
    now = datetime.now().isoformat(timespec="seconds")
    with history_db() as conn:
        exists = conn.execute("SELECT phone FROM contacts WHERE phone=?", (normalized,)).fetchone()
        if not exists:
            return jsonify({"error": "Kisi bulunamadi."}), 404
        upsert_contact(conn, normalized, opt_out=data["opt_out"])
        conn.execute("UPDATE contacts SET updated_at=? WHERE phone=?", (now, normalized))
        row = conn.execute("SELECT phone,name,opt_out,last_sent_at FROM contacts WHERE phone=?", (normalized,)).fetchone()
    log(f"Mesaj istemiyor durumu guncellendi: {normalized} ({data['opt_out']})",
        event="contact_opt_out", phone=normalized)
    return jsonify({"ok": True, "item": dict(row)})


@app.get("/api/messages")
def api_messages():
    with history_db() as conn:
        rows = conn.execute("SELECT id, name, message, created_at FROM saved_messages ORDER BY created_at DESC, id DESC").fetchall()
    return jsonify({"items": [dict(row) for row in rows]})


@app.post("/api/messages")
def api_save_message():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Gecersiz istek govdesi."}), 400
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Kaydedilecek mesaj bos olamaz."}), 400
    message_id = ensure_saved_message(message, data.get("name", ""), data.get("id", ""))
    with history_db() as conn:
        item = conn.execute("SELECT id, name, message, created_at FROM saved_messages WHERE id=?", (message_id,)).fetchone()
    return jsonify({"ok": True, "item": dict(item)})


@app.get("/api/templates")
def api_templates():
    with history_db() as conn:
        backfill_template_history(conn)
        rows = conn.execute("""SELECT t.id, t.name, t.message, t.message_id, t.image, t.created_at, t.updated_at,
            (SELECT COUNT(DISTINCT h.phone) FROM send_history h
             WHERE h.template_id=t.id AND h.result IN ('ok','belirsiz')) AS sent_recipients
            FROM message_templates t ORDER BY t.updated_at DESC, t.name COLLATE NOCASE""").fetchall()
    return jsonify({"items": [dict(row) for row in rows]})


@app.post("/api/templates")
def api_create_template():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Gecersiz istek govdesi."}), 400
    name = (data.get("name") or "").strip()
    message = (data.get("message") or "").strip()
    image = clean_image_name(data.get("image"))
    if data.get("image") and not image:
        return jsonify({"error": "Sablon resmi bulunamadi."}), 400
    if not name or not message:
        return jsonify({"error": "Sablon adi ve mesaj metni gerekli."}), 400
    template_id = uuid.uuid4().hex
    message_id = ensure_saved_message(message, name)
    now = datetime.now().isoformat(timespec="seconds")
    with history_db() as conn:
        conn.execute("INSERT INTO message_templates (id,name,message,message_id,image,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                     (template_id, name, message, message_id, image, now, now))
        conn.execute("UPDATE send_history SET template_id=? WHERE message_id=? AND template_id=''",
                     (template_id, message_id))
    return jsonify({"ok": True, "item": {"id": template_id, "name": name, "message": message, "message_id": message_id, "image": image,
                                             "created_at": now, "updated_at": now, "sent_recipients": 0}})


@app.put("/api/templates/<template_id>")
def api_update_template(template_id):
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Gecersiz istek govdesi."}), 400
    name = (data.get("name") or "").strip()
    message = (data.get("message") or "").strip()
    image = clean_image_name(data.get("image"))
    if data.get("image") and not image:
        return jsonify({"error": "Sablon resmi bulunamadi."}), 400
    if not name or not message:
        return jsonify({"error": "Sablon adi ve mesaj metni gerekli."}), 400
    with history_db() as conn:
        exists = conn.execute("SELECT id,message_id FROM message_templates WHERE id=?", (template_id,)).fetchone()
    if not exists:
        return jsonify({"error": "Sablon bulunamadi."}), 404
    # Sablon kimligi icerik degisse de sabit kalir; onceki alicilar tekrar gonderimden korunur.
    message_id = exists["message_id"]
    now = datetime.now().isoformat(timespec="seconds")
    with history_db() as conn:
        conn.execute("UPDATE message_templates SET name=?, message=?, image=?, updated_at=? WHERE id=?",
                     (name, message, image, now, template_id))
    with lock:
        for job in jobs:
            if job.get("template_id") != template_id or job.get("allow_resend"):
                continue
            job["message_id"] = message_id
            for recipient in job.get("recipients", []):
                if recipient.get("template_id") == template_id:
                    recipient["message_id"] = message_id
        save_jobs()
        load_history()
    return jsonify({"ok": True, "item": {"id": template_id, "name": name, "message": message,
                                             "message_id": message_id, "image": image, "updated_at": now}})


@app.delete("/api/templates/<template_id>")
def api_delete_template(template_id):
    with history_db() as conn:
        cursor = conn.execute("DELETE FROM message_templates WHERE id=?", (template_id,))
        if not cursor.rowcount:
            return jsonify({"error": "Sablon bulunamadi."}), 404
    return jsonify({"ok": True})


@app.get("/api/templates/<template_id>/recipients")
def api_template_recipients(template_id):
    with history_db() as conn:
        backfill_template_history(conn)
        template = conn.execute("SELECT id,name FROM message_templates WHERE id=?", (template_id,)).fetchone()
        if not template:
            return jsonify({"error": "Sablon bulunamadi."}), 404
        rows = conn.execute("""SELECT phone,name,message,sent_at,result,error,job_name
            FROM send_history WHERE template_id=? ORDER BY id DESC""", (template_id,)).fetchall()
    return jsonify({"name": template["name"], "items": [dict(row) for row in rows]})


@app.post("/api/jobs")
def api_add():
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return jsonify({"error": "Gecersiz istek govdesi."}), 400
    recipients = parse_recipients(d.get("phones", ""))
    message = (d.get("message") or "").strip()
    image = clean_image_name(d.get("image"))
    if not recipients or (not message and not image):
        return jsonify({"error": "Numara ve mesaj (veya resim) gerekli."}), 400
    start = (d.get("start") or "").strip()
    try:
        start_dt = datetime.fromisoformat(start) if start else datetime.now()
    except ValueError:
        return jsonify({"error": "Tarih formati hatali."}), 400
    try:
        count = max(1, int(d.get("count") or 1))
        min_d = max(3, int(d.get("min_delay") or 8))
        max_d = max(min_d, int(d.get("max_delay") or 15))
        interval = max(1, int(d.get("interval") or 60))
    except (TypeError, ValueError):
        return jsonify({"error": "Tur, bekleme ve aralik alanlari sayi olmali."}), 400
    job_name = (d.get("name") or "").strip() or f"Gorev {len(jobs) + 1}"
    template_id = (d.get("template_id") or "").strip()
    with history_db() as conn:
        selected_template = conn.execute("SELECT message,message_id,image FROM message_templates WHERE id=?", (template_id,)).fetchone() if template_id else None
    if not selected_template or selected_template["message"] != message:
        template_id = ""
        message_id = ensure_saved_message(message, job_name, d.get("message_id", ""))
    else:
        message_id = selected_template["message_id"]
        image = selected_template["image"]
    job = {
        "id": uuid.uuid4().hex[:8],
        "type": "tur",
        "name": job_name,
        "recipients": [],
        "skipped": [],
        "message": message,
        "message_id": message_id,
        "template_id": template_id,
        "image": image,
        "next_run": start_dt.isoformat(timespec="seconds"),
        "remaining": count,
        "total": count,
        "interval_min": interval,
        "min_delay": min_d,
        "max_delay": max_d,
        "status": "bekliyor",
        "sent": 0,
        "failed": 0,
        "note": "",
    }
    with lock:
        for r in recipients:
            r["message_id"] = message_id
            r["template_id"] = template_id
        job["recipients"], job["skipped"] = phone_blocks(recipients, message, image)
        if not job["recipients"]:
            job["status"] = "tamamlandi"
            job["next_run"] = ""
        jobs.append(job)
        save_jobs()
    log(f"Gorev eklendi: {job['name']} ({len(job['recipients'])} alici, mesaj hazir).",
        event="job_queued", job_id=job.get("id"))
    return jsonify({"ok": True, "message_id": message_id})


@app.post("/api/jobs/csv")
def api_add_csv():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "CSV dosyasi secilmedi."}), 400
    recipients, skipped, meta = parse_csv(f.read())
    form = request.form
    if "send_phones" in form:
        try:
            selected_phones = json.loads(form.get("send_phones", "[]"))
        except (TypeError, json.JSONDecodeError):
            return jsonify({"error": "Gonderilecek kisiler listesi gecersiz."}), 400
        if not isinstance(selected_phones, list) or any(not isinstance(phone, str) for phone in selected_phones):
            return jsonify({"error": "Gonderilecek kisiler listesi gecersiz."}), 400
        selected = set(selected_phones)
        with history_db() as conn:
            opted_out = {row["phone"] for row in conn.execute("SELECT phone FROM contacts WHERE opt_out=1")}
        kept = []
        for recipient in recipients:
            if recipient["phone"] in selected:
                kept.append(recipient)
            else:
                skipped.append({"row": recipient.get("row", "-"), "name": recipient.get("name", ""),
                                "raw": recipient["phone"],
                                "reason": "kisi mesaj almak istemiyor" if recipient["phone"] in opted_out else "kullanici bu kisiyi atla olarak secti",
                                "src": recipient.get("src")})
        recipients = kept
        if not recipients:
            return jsonify({"error": "Gonderilecek kisi secilmedi."}), 400
    elif not recipients:
        return jsonify({"error": "CSV'de gecerli telefon numarasi bulunamadi."}), 400
    message = (form.get("message") or "").strip()
    image = clean_image_name(form.get("image"))
    if not message and not image and any(not r["message"] for r in recipients):
        return jsonify({"error": "Bir mesaj yaz, resim ekle (veya CSV'de her satira mesaj sutunu ekle)."}), 400
    try:
        limit = max(1, int(form.get("daily_limit") or 200))
        ws = min(max(int(form.get("win_start") or 9), 0), 23)
        we = min(max(int(form.get("win_end") or 21), 1), 24)
        min_d = max(3, int(form.get("min_delay") or 60))
        max_d = max(min_d, int(form.get("max_delay") or 120))
    except ValueError:
        return jsonify({"error": "Sayi alanlarini kontrol et."}), 400
    if we <= ws:
        return jsonify({"error": "Bitis saati, baslangic saatinden buyuk olmali."}), 400
    start = (form.get("start") or "").strip()
    try:
        start_dt = datetime.fromisoformat(start) if start else datetime.now()
    except ValueError:
        return jsonify({"error": "Tarih formati hatali."}), 400
    name = (form.get("name") or "").strip() or Path(f.filename or "liste").stem
    template_id = (form.get("template_id") or "").strip()
    with history_db() as conn:
        selected_template = conn.execute("SELECT message,message_id,image FROM message_templates WHERE id=?", (template_id,)).fetchone() if template_id else None
    if not selected_template or selected_template["message"] != message:
        template_id = ""
        message_id = ensure_saved_message(message, name, form.get("message_id", ""))
    else:
        message_id = selected_template["message_id"]
        image = selected_template["image"]
    original_message_id = message_id
    job = {
        "id": uuid.uuid4().hex[:8],
        "type": "liste",
        "name": name,
        "recipients": [],
        "skipped": skipped[:20000],
        "src_header": meta.get("src_header"),
        "message": message,
        "message_id": message_id,
        "template_id": template_id,
        "allow_resend": False,
        "image": image,
        "next_run": start_dt.isoformat(timespec="seconds"),
        "status": "bekliyor",
        "sent": 0,
        "failed": 0,
        "daily_limit": limit,
        "sent_today": 0,
        "day": "",
        "win_start": ws,
        "win_end": we,
        "spread": form.get("spread") == "true",
        "min_delay": min_d,
        "max_delay": max_d,
        "note": "",
    }
    with lock:
        for r in recipients:
            template = (r.get("message") or message or "").strip()
            r["template_id"] = template_id if template_id and template == message else ""
            source_id = (original_message_id if r["template_id"] else
                         ensure_saved_message(template, name, "") if template else "")
            r["message_id"] = source_id
        job["recipients"], blocked = phone_blocks(recipients, message, image)
        job["skipped"] = (job["skipped"] + blocked)[-20000:]
        if not job["recipients"]:
            job["status"] = "tamamlandi"
            job["next_run"] = ""
        jobs.append(job)
        save_jobs()
    total_skipped = len(job["skipped"])
    message_ids = sorted({r.get("message_id") for r in job["recipients"] if r.get("message_id")})
    log(f"CSV islendi: {len(recipients)} alici kuyruga eklendi, {total_skipped} kayit atlandi; gunluk limit {limit}.",
        event="csv_import", job_id=job.get("id"))
    log(f"Liste gonderime hazir: {name}.", event="job_queued", job_id=job.get("id"))
    info = f"{len(job['recipients'])} kisi eklendi, {total_skipped} satir atlandi."
    if message_id:
        info += f" Mesaj ID: {message_id}."
    elif message_ids:
        info += f" CSV mesajlari ID ile kaydedildi ({len(message_ids)} farkli mesaj)."
    if total_skipped:
        info += " Atlananlari gormek icin gorevin yanindaki Detay > Atlanan satirlar sekmesine bak."
    return jsonify({"ok": True, "info": info, "message_id": message_id, "message_ids": message_ids})


@app.post("/api/csv/preview")
def api_csv_preview():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "CSV dosyasi secilmedi."}), 400
    recipients, skipped, _ = parse_csv(f.read())
    with history_db() as conn:
        opted_out = {row["phone"] for row in conn.execute("SELECT phone FROM contacts WHERE opt_out=1")}
    items = [{"phone": r["phone"], "name": r.get("name", ""), "row": r.get("row", "-"),
              "opt_out": r["phone"] in opted_out} for r in recipients]
    return jsonify({
        "items": items,
        "skipped_count": len(skipped),
        "opted_out_count": sum(1 for item in items if item["opt_out"]),
    })


@app.get("/api/jobs/<job_id>/recipients")
def api_recipients(job_id):
    with lock:
        job = find_job(job_id)
        if not job:
            return jsonify({"error": "Gorev bulunamadi."}), 404
        recs = [{
            "phone": r["phone"],
            "name": r.get("name", ""),
            "status": r.get("status", "-"),
            "error": r.get("error", ""),
            "time": r.get("time", ""),
            "message": (r.get("message") or job.get("message") or "").replace("{isim}", r.get("name") or ""),
            "message_id": r.get("message_id") or job.get("message_id", ""),
        } for r in job["recipients"]]
        return jsonify({
            "name": job["name"],
            "type": job.get("type", "tur"),
            "recipients": recs,
            "skipped": [{k: v for k, v in s.items() if k != "src"} for s in job.get("skipped", [])],
        })


def _set(row, i, value):
    while len(row) <= i:
        row.append("")
    row[i] = value


def google_rows(job, only_problems):
    """Listeyi Google Kisiler iceri aktarma formatinda dondurur: (baslik, satirlar).
    Orijinal Google satirlari korunur; numaralar +90... biciminde guncellenir; sorunlu kisiler
    Labels sutununa eklenen etiketlerle isaretlenir. lock icinde cagrilmali."""
    header = list(job.get("src_header") or GOOGLE_COLS)
    hl = [h.strip().lower() for h in header]
    if "labels" not in hl:
        header.append("Labels")
        hl.append("labels")
    li = hl.index("labels")
    first_i = hl.index("first name") if "first name" in hl else None
    plabel_i = hl.index("phone 1 - label") if "phone 1 - label" in hl else None
    pval_i = hl.index("phone 1 - value") if "phone 1 - value" in hl else None
    has_src = bool(job.get("src_header"))

    def with_labels(row, labs):
        if not labs:
            return
        cur = row[li].strip() if len(row) > li else ""
        parts = [p.strip() for p in cur.split(":::") if p.strip()]
        for lab in labs:
            if lab not in parts:
                parts.append(lab)
        _set(row, li, " ::: ".join(parts))

    items = []  # (siralama anahtari, satir)
    for r in job["recipients"]:
        st = r.get("status", "-")
        labs = []
        if st == "gecersiz":
            labs.append(LBL_INVALID)
        elif st == "hata":
            labs.append(LBL_ERROR)
        elif st == "daha_once_gonderildi":
            labs.append(LBL_SENT_BEFORE)
        elif r.get("orig_phone"):
            labs.append(LBL_FIXED)
        if only_problems and not labs:
            continue
        number = "+" + r["phone"]
        if has_src and r.get("src") is not None:
            row = list(r["src"])
            pc, pp = r.get("pc"), r.get("pp", 0)
            if pc is not None:  # numarayi okundugu hucreye geri yaz (cok degerli hucrede sadece o parcayi)
                parts = [p.strip() for p in (row[pc] if pc < len(row) else "").split(":::")]
                while len(parts) <= pp:
                    parts.append("")
                parts[pp] = number
                _set(row, pc, " ::: ".join(parts))
            elif pval_i is not None:
                _set(row, pval_i, number)
        else:
            row = []
            if first_i is not None:
                _set(row, first_i, r.get("name", ""))
            if plabel_i is not None:
                _set(row, plabel_i, "Mobile")
            if pval_i is not None:
                _set(row, pval_i, number)
        with_labels(row, labs)
        items.append((r["row"] if isinstance(r.get("row"), int) else 10**9, row))

    for s in job.get("skipped", []):
        reason = s.get("reason", "")
        if reason.startswith("telefon yok"):
            if only_problems:
                continue
            labs = []
        elif reason.startswith("gecersiz"):
            labs = [LBL_INVALID]
        elif reason.startswith("tekrar"):
            labs = [LBL_DUP]
        elif reason.startswith(("daha once mesaj gonderildi", "bu mesaj daha once gonderildi")):
            labs = [LBL_SENT_BEFORE]
        else:  # 'listede zaten var' gibi: orijinal satiri elimizde yok
            continue
        if has_src and s.get("src") is not None:
            row = list(s["src"])
        else:
            row = []
            if first_i is not None:
                _set(row, first_i, s.get("name", ""))
            if pval_i is not None:
                _set(row, pval_i, s.get("raw", ""))
        with_labels(row, labs)
        items.append((s["row"] if isinstance(s.get("row"), int) else 10**9, row))

    items.sort(key=lambda x: x[0])
    width = len(header)
    return header, [r + [""] * (width - len(r)) for _, r in items]


@app.post("/api/upload")
def api_upload():
    """Mesaja eklenecek resmi kaydeder. Dosya turu uzantiya degil icerige bakilarak belirlenir."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "Dosya gelmedi."}), 400
    data = f.read()
    if len(data) > 15 * 1024 * 1024:
        return jsonify({"error": "Resim 15 MB'dan buyuk."}), 400
    ext = None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = "png"
    elif data.startswith(b"\xff\xd8\xff"):
        ext = "jpg"
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        ext = "gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = "webp"
    if not ext:
        return jsonify({"error": "Sadece PNG, JPG, GIF veya WEBP resim eklenebilir."}), 400
    IMAGES_DIR.mkdir(exist_ok=True)
    name = f"{uuid.uuid4().hex}.{ext}"
    (IMAGES_DIR / name).write_bytes(data)
    return jsonify({"ok": True, "name": name, "url": f"/images/{name}"})


@app.get("/images/<name>")
def serve_image(name):
    if not IMAGE_RE.match(name):
        return "", 404
    return send_from_directory(str(IMAGES_DIR), name)


@app.get("/api/jobs/<job_id>/export")
def api_export(job_id):
    which = request.args.get("which", "all")
    with lock:
        job = find_job(job_id)
        if not job:
            return jsonify({"error": "Gorev bulunamadi."}), 404
        if which.startswith("google_"):
            header, grows = google_rows(job, only_problems=(which == "google_sorunlu"))
            recs, skipped = [], []
        else:
            recs = [dict(r) for r in job["recipients"]]
            skipped = [dict(s) for s in job.get("skipped", [])]
    if which.startswith("google_"):
        gbuf = io.StringIO()
        gw = csv.writer(gbuf)  # Google dosyasi virgullu olmali (BOM yok)
        gw.writerow(header)
        gw.writerows(grows)
        return Response(gbuf.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="google_kisiler_{job_id}_{which[7:]}.csv"'})
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")  # Turkce Excel ';' ayiracini dogrudan acar
    if which == "atlanan":
        w.writerow(["satir", "isim", "girilen_deger", "neden"])
        for s in skipped:
            w.writerow([s["row"], s["name"], s["raw"], s["reason"]])
    else:
        w.writerow(["telefon", "isim", "durum", "aciklama", "zaman"])
        for r in recs:
            st = r.get("status", "-")
            if which == "sorunlu" and st not in ("gecersiz", "hata"):
                continue
            w.writerow([r["phone"], r.get("name", ""), st, r.get("error", ""), r.get("time", "")])
    data = "﻿" + buf.getvalue()  # BOM: Excel Turkce karakterleri dogru gostersin
    fname = f"liste_{job_id}_{which}.csv"
    return Response(data, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/api/jobs/<job_id>/retry")
def api_retry(job_id):
    """Yalnizca gecici 'hata' durumundaki alicilari tekrar siraya alir; gecersiz numara duzeltilmelidir."""
    d = request.get_json(silent=True) or {}
    if not isinstance(d, dict):
        return jsonify({"error": "Gecersiz istek govdesi."}), 400
    phone = d.get("phone")
    message_id = d.get("message_id")
    n = 0
    with lock:
        job = find_job(job_id)
        if not job or job.get("type") != "liste":
            return jsonify({"error": "Gorev bulunamadi."}), 404
        for r in job["recipients"]:
            if r["status"] != "hata":
                continue
            if phone is not None and r["phone"] != phone:
                continue
            if message_id and r.get("message_id") != message_id:
                continue
            r["status"] = "bekliyor"
            r["error"] = ""
            r["time"] = ""
            r["attempts"] = 0
            n += 1
        if n:
            wake(job)
            save_jobs()
            log(f"{n} alici hata durumundan yeniden siraya alindi.", event="job_retry", job_id=job_id)
    return jsonify({"ok": True, "n": n})


@app.post("/api/jobs/<job_id>/recipient/edit")
def api_rec_edit(job_id):
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return jsonify({"error": "Gecersiz istek govdesi."}), 400
    new = normalize_phone(d.get("new_phone", ""))
    if not new:
        return jsonify({"error": "Gecerli bir numara gir (ulke koduyla, ornek 905551234567)."}), 400
    with lock:
        job = find_job(job_id)
        if not job or job.get("type") != "liste":
            return jsonify({"error": "Gorev bulunamadi."}), 404
        rec = next((r for r in job["recipients"] if r["phone"] == d.get("phone")
                    and (not d.get("message_id") or r.get("message_id") == d.get("message_id"))), None)
        if not rec:
            return jsonify({"error": "Kisi bulunamadi."}), 404
        rec_key = message_fingerprint(rec.get("message") or job.get("message", ""), job.get("image", ""))
        if new != rec["phone"] and any(
            r["phone"] == new and message_fingerprint(r.get("message") or job.get("message", ""), job.get("image", "")) == rec_key
            for r in job["recipients"]
        ):
            return jsonify({"error": "Bu numara ve ayni mesaj listede zaten var."}), 400
        if new != rec["phone"] and not rec.get("orig_phone"):
            rec["orig_phone"] = rec["phone"]  # Google'a "numara duzeltildi" diye isaretlemek icin
        rec["phone"] = new
        if rec["status"] in ("gecersiz", "hata"):  # duzeltilen numara yeniden denensin
            rec["status"] = "bekliyor"
            rec["error"] = ""
            rec["time"] = ""
            rec["attempts"] = 0
            wake(job)
        save_jobs()
    if job:
        log(f"Alici numarasi guncellendi: {d.get('phone')} -> {new}.",
            event="recipient_updated", job_id=job_id, phone=new)
    return jsonify({"ok": True})


@app.post("/api/jobs/<job_id>/recipient/delete")
def api_rec_delete(job_id):
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return jsonify({"error": "Gecersiz istek govdesi."}), 400
    with lock:
        job = find_job(job_id)
        if not job or job.get("type") != "liste":
            return jsonify({"error": "Gorev bulunamadi."}), 404
        message_id = d.get("message_id")
        job["recipients"] = [r for r in job["recipients"] if not (
            r["phone"] == d.get("phone") and (not message_id or r.get("message_id") == message_id)
        )]
        save_jobs()
    return jsonify({"ok": True})


@app.post("/api/jobs/<job_id>/append")
def api_append(job_id):
    """Guncel bir CSV yuklenir: listede olmayan yeni kisiler eklenir, mevcutlar atlanir."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "CSV dosyasi secilmedi."}), 400
    new, skipped, meta = parse_csv(f.read())
    with lock:
        job = find_job(job_id)
        if not job or job.get("type") != "liste":
            return jsonify({"error": "Gorev bulunamadi."}), 404
        # Ek dosyanin basligi ilk dosyayla ayniysa orijinal satirlar korunur (Google'a geri aktarim icin)
        same_src = bool(job.get("src_header")) and meta.get("src_header") == job.get("src_header")
        for r in new:
            template = (r.get("message") or job["message"] or "").strip()
            r["message_id"] = ensure_saved_message(template, job["name"], job.get("message_id", "")) if template else ""
            r["template_id"] = job.get("template_id", "") if template == job.get("message", "").strip() else ""
        existing = {(r["phone"], message_identity(
                        r.get("message_id") or job.get("message_id", ""),
                        message_fingerprint(r.get("message") or job["message"], job.get("image", ""))))
                    for r in job["recipients"]}
        new, globally_skipped = phone_blocks(new, job["message"], job.get("image", ""), exclude_job_id=job_id)
        skipped.extend(globally_skipped)
        for s in skipped:
            s["row"] = f"ek:{s['row']}"
            if not same_src:
                s.pop("src", None)
        eligible = []
        for r in new:
            pair = (r["phone"], message_identity(
                        r.get("message_id") or job.get("message_id", ""),
                        message_fingerprint(r.get("message") or job["message"], job.get("image", ""))))
            if pair in existing:
                skipped.append({"row": r.get("row", "-"), "name": r["name"], "raw": r["phone"],
                                "reason": "listede ayni mesaj zaten var"})
                continue
            eligible.append(r)
            existing.add(pair)
        if not job["message"] and not job.get("image") and any(not r["message"] for r in eligible):
            return jsonify({"error": "Bu listenin ortak mesaji yok; yeni kisilerin CSV'de mesaj sutunu olmali."}), 400
        added = 0
        for r in eligible:
            r.pop("row", None)
            if not same_src:
                for k in ("src", "pc", "pp"):
                    r.pop(k, None)
            job["recipients"].append(r)
            added += 1
        job["skipped"] = (job.get("skipped", []) + skipped)[-20000:]
        if added:
            wake(job)
        save_jobs()
    log(f"Ek CSV islendi: {added} yeni kisi eklendi, {len(skipped)} kayit atlandi.",
        event="csv_append", job_id=job.get("id"))
    return jsonify({"ok": True, "info": f"{added} yeni kisi eklendi, {len(skipped)} satir atlandi."})


@app.post("/api/jobs/<job_id>/toggle")
def api_toggle(job_id):
    action = ""
    job_name = ""
    with lock:
        for j in jobs:
            if j["id"] == job_id:
                if j["status"] in ("bekliyor", "calisiyor"):
                    j["status"] = "duraklatildi"
                    action = "duraklatildi"
                elif j["status"] == "duraklatildi":
                    j["status"] = "bekliyor"
                    j["next_run"] = datetime.now().isoformat(timespec="seconds")
                    action = "devam ediyor"
                job_name = j["name"]
                save_jobs()
                break
    if action:
        log(f"Gorev {action}: {job_name}.", event="job_control", job_id=job_id)
    return jsonify({"ok": True})


@app.delete("/api/jobs/<job_id>")
def api_delete(job_id):
    with lock:
        gone = [j for j in jobs if j["id"] == job_id]
        jobs[:] = [j for j in jobs if j["id"] != job_id]
        for g in gone:  # baska gorev kullanmiyorsa resim dosyasini da sil
            img = g.get("image")
            if img and IMAGE_RE.match(img) and not any(j.get("image") == img for j in jobs):
                try:
                    (IMAGES_DIR / img).unlink()
                except OSError:
                    pass
        save_jobs()
    for job in gone:
        log(f"Gorev silindi: {job['name']}.", event="job_deleted", job_id=job_id)
    return jsonify({"ok": True})


HTML = (BASE / "templates" / "index.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    if not acquire_single_instance():
        print("WhatsAppTransporter zaten calisiyor; ikinci uygulama acilisi engellendi.")
        raise SystemExit(1)
    load_jobs()
    load_history()
    threading.Thread(target=worker, daemon=True).start()
    threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:5001")).start()
    serve(app, listen="127.0.0.1:5001", threads=8,
          max_request_body_size=25 * 1024 * 1024, expose_tracebacks=False)
