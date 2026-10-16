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

from flask import Flask, Response, jsonify, request, send_from_directory
from playwright.sync_api import sync_playwright

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
    # Persist independently of load_history(): startup recovery can log before
    # the rest of the history schema is initialized.
    try:
        with history_db() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS app_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL, level TEXT NOT NULL DEFAULT 'INFO',
                event TEXT NOT NULL DEFAULT 'runtime', job_id TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '', message TEXT NOT NULL
            )""")
            conn.execute("""INSERT INTO app_logs
                (created_at, level, event, job_id, phone, message) VALUES (?, ?, ?, ?, ?, ?)""",
                (created_at, level, event, job_id or "", phone or "", str(msg)))
    except Exception as exc:
        print(f"Log veritabanina yazilamadi: {exc}")


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


def message_identity(message_id, message_key):
    """Kayitli mesaj ID'sini tekrar gonderim icin asil kimlik yapar."""
    return message_id or f"legacy:{message_key}"


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
    bypass_job_ids = {job.get("id") for job in jobs if job.get("allow_resend") and job.get("id")}
    with history_db() as conn:
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_send_history_time ON send_history(sent_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS app_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL, level TEXT NOT NULL DEFAULT 'INFO',
            event TEXT NOT NULL DEFAULT 'runtime', job_id TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '', message TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_app_logs_created_at ON app_logs(created_at)")
        for template in conn.execute("SELECT id,name,message FROM message_templates WHERE message_id='' ").fetchall():
            stable_id = ensure_saved_message(template["message"], template["name"], conn=conn)
            conn.execute("UPDATE message_templates SET message_id=? WHERE id=?", (stable_id, template["id"]))
        # Sablon eski surumlerde duzenlenince yeni message_id aliyordu. Sablon ID'si
        # sabit oldugundan o gonderimleri mevcut kalici kimlige tasiyip yeniden gonderimi engelle.
        if bypass_job_ids:
            placeholders = ",".join("?" for _ in bypass_job_ids)
            conn.execute(f"""UPDATE send_history
                SET message_id=(SELECT t.message_id FROM message_templates t WHERE t.id=send_history.template_id)
                WHERE template_id<>'' AND EXISTS(SELECT 1 FROM message_templates t WHERE t.id=send_history.template_id)
                  AND message_id<>(SELECT t.message_id FROM message_templates t WHERE t.id=send_history.template_id)
                  AND job_id NOT IN ({placeholders})""", tuple(bypass_job_ids))
        else:
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
        for row in conn.execute("SELECT id, phone, message, image, job_id FROM send_history WHERE message_key='' ").fetchall():
            old_job = old_jobs.get(row["job_id"], {})
            old_rec = next((r for r in old_job.get("recipients", []) if r.get("phone") == row["phone"]), {})
            template = (old_rec.get("message") or old_job.get("message") or row["message"]).strip()
            image = row["image"] or old_job.get("image", "")
            message_id = ensure_saved_message(template, old_job.get("name", ""), old_job.get("message_id", ""), conn)
            conn.execute("UPDATE send_history SET message_key=?, message_id=? WHERE id=?",
                         (message_fingerprint(template, image), message_id, row["id"]))
        # Onceki surumlerde fingerprint kaydedilmis ama mesaj ID'si bos kalmis olabilir.
        for row in conn.execute("SELECT id, phone, message, image, job_id FROM send_history WHERE message_id='' ").fetchall():
            old_job = old_jobs.get(row["job_id"], {})
            old_rec = next((r for r in old_job.get("recipients", []) if r.get("phone") == row["phone"]), {})
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
        rows = conn.execute("SELECT * FROM send_history WHERE result IN ('ok', 'belirsiz') ORDER BY id").fetchall()
        sent_message_map = {
            (row["phone"], message_identity(row["message_id"], row["message_key"])): dict(row)
            for row in rows
        }
        if jobs_changed:
            save_jobs()


def phone_blocks(recipients, message="", image="", exclude_job_id=None, bypass_sent=False):
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

    accepted, skipped, seen = [], [], set()
    for r in recipients:
        phone = r.get("phone", "")
        template = (r.get("message") or message or "").strip()
        message_key = message_fingerprint(template, image)
        history_pair = (phone, message_identity(r.get("queue_message_id") or r.get("message_id"), message_key))
        if history_pair in seen:
            skipped.append({"row": r.get("row", "-"), "name": r.get("name", ""), "raw": phone,
                            "reason": "ayni yuklemede tekrar", "src": r.get("src")})
        elif not bypass_sent and history_pair in sent_message_map:
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


def normalize_phone(raw):
    d = "".join(ch for ch in str(raw) if ch.isdigit())
    if not d:
        return ""
    if d.startswith("00"):
        d = d[2:]
    if len(d) == 11 and d.startswith("0"):  # 05551234567 -> 905551234567
        d = "90" + d[1:]
    elif len(d) == 10 and d.startswith("5"):  # 5551234567 -> 905551234567
        d = "90" + d
    return d if 10 <= len(d) <= 15 else ""


def parse_recipients(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",", 1)
        phone = normalize_phone(parts[0])
        name = parts[1].strip() if len(parts) > 1 else ""
        if phone:
            out.append({"phone": phone, "name": name})
    return out


PHONE_KEYS = {"phone", "telefon", "numara", "tel", "gsm", "no", "telefon no"}
NAME_KEYS = {"name", "isim", "ad", "adi", "ad soyad", "adsoyad", "isim soyisim"}
MSG_KEYS = {"message", "mesaj"}


def parse_google_contacts(header, rows, first_row_no):
    """Google Contacts disa aktarma formati: telefon 'Phone 1 - Value' sutununda.
    (alici_listesi, atlanan_satirlar) dondurur."""
    head = [c.strip().lower() for c in header]

    def idx(name):
        return head.index(name) if name in head else None

    phone_cols = [i for i, h in enumerate(head) if h.startswith("phone ") and h.endswith(" - value")]
    phone_cols.sort(key=lambda i: int(head[i].split()[1]) if head[i].split()[1].isdigit() else 99)
    first, last = idx("first name"), idx("last name")
    nick, org = idx("nickname"), idx("organization name")

    def col(r, i):
        return r[i].strip() if i is not None and i < len(r) else ""

    out, seen, skipped = [], set(), []
    for n, r in enumerate(rows, start=first_row_no):
        raws = [col(r, pc) for pc in phone_cols]
        full = " ".join(x for x in (col(r, first), col(r, last)) if x) or col(r, nick) or col(r, org)
        raw_show = next((v for v in raws if v), "")
        valid = []
        for pc, raw in zip(phone_cols, raws):
            for pi_, part in enumerate(raw.split(":::")):
                phone = normalize_phone(part)
                if phone:
                    valid.append((phone, pc, pi_))
        if not valid:
            reason = "telefon yok" if not raw_show else "gecersiz numara"
            skipped.append({"row": n, "name": full, "raw": raw_show[:60], "reason": reason, "src": list(r)})
            continue
        name = col(r, first) or col(r, nick) or col(r, last) or col(r, org)
        added = False
        for phone, used_col, used_part in valid:
            if phone in seen:
                skipped.append({"row": n, "name": full, "raw": phone,
                                "reason": "tekrar (ayni numara listede var)", "src": list(r)})
                continue
            seen.add(phone)
            added = True
            out.append({"phone": phone, "name": name, "message": "", "status": "bekliyor", "error": "", "time": "",
                        "row": n, "src": list(r), "pc": used_col, "pp": used_part})
        if not added and not any(s.get("row") == n for s in skipped):
            skipped.append({"row": n, "name": full, "raw": raw_show[:60],
                            "reason": "tekrar (ayni numara listede var)", "src": list(r)})
    return out, skipped, {"src_header": list(header)}


def parse_csv(raw_bytes):
    """CSV'den alicilari okur. (alici_listesi, atlanan_satirlar, meta) dondurur.
    meta: Google dosyasiysa {"src_header": [...]}, degilse {}.
    Atlanan satir: {"row", "name", "raw", "reason"}"""
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    if "phone 1 - value" in text[:3000].lower():
        dialect = csv.excel  # Google Contacts dosyasi her zaman virgullu
    else:
        try:
            dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not rows:
        return [], [], {}
    head = [c.strip().lower() for c in rows[0]]
    if "phone 1 - value" in head:
        return parse_google_contacts(rows[0], rows[1:], 2)
    pi, ni, mi = 0, 1, 2
    first_row_no = 1
    if any(h in PHONE_KEYS | NAME_KEYS | MSG_KEYS for h in head):
        pi = next((i for i, h in enumerate(head) if h in PHONE_KEYS), 0)
        ni = next((i for i, h in enumerate(head) if h in NAME_KEYS), None)
        mi = next((i for i, h in enumerate(head) if h in MSG_KEYS), None)
        rows = rows[1:]
        first_row_no = 2

    def col(r, i):
        return r[i].strip() if i is not None and i < len(r) else ""

    out, seen, skipped = [], set(), []
    for n, r in enumerate(rows, start=first_row_no):
        raw = col(r, pi)
        phone = normalize_phone(raw)
        if not phone:
            reason = "telefon yok" if not raw else "gecersiz numara"
            skipped.append({"row": n, "name": col(r, ni), "raw": raw[:60], "reason": reason})
            continue
        row_message = col(r, mi)
        pair = (phone, message_fingerprint(row_message))
        if pair in seen:
            skipped.append({"row": n, "name": col(r, ni), "raw": raw[:60], "reason": "tekrar (ayni numara ve mesaj listede var)"})
            continue
        seen.add(pair)
        out.append({"phone": phone, "name": col(r, ni), "message": row_message,
                    "status": "bekliyor", "error": "", "time": "", "row": n})
    return out, skipped, {}


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


def send_image(page, phone, caption, image_path):
    """Resmi sohbete yapistirir (Ctrl+V gibi), yazi varsa baslik olarak ekler ve gonderir."""
    image_path = Path(image_path)
    mime = mimetypes.guess_type(str(image_path))[0] or "image/png"
    b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    try:
        page.evaluate(PASTE_JS, {"sel": INPUT_SELECTOR, "b64": b64, "mime": mime, "name": image_path.name})
    except Exception as e:
        return "hata", f"Resim gonderime hazirlanamadi: {e}"
    try:
        page.wait_for_selector(PREVIEW_SEND_SELECTOR, timeout=15000)
    except Exception:
        snap(page, phone, "onizleme_yok")
        return "hata", "Resim onizlemesi acilmadi (WhatsApp arayuzu degismis olabilir; hata_ekran klasorundeki goruntuye bak)"
    time.sleep(1.0)
    if caption:
        for i, line in enumerate(caption.split("\n")):
            if i:
                page.keyboard.press("Shift+Enter")
            if line:
                page.keyboard.insert_text(line)
        time.sleep(0.5)
    try:
        # Enter gonderimi baslatabilir; bu noktadan sonraki her hata belirsizdir.
        page.keyboard.press("Enter")
        # gonderilince onizleme kapanir ve gonder dugmesi kaybolur
        page.wait_for_selector(PREVIEW_SEND_SELECTOR, state="detached", timeout=20000)
    except Exception:
        snap(page, phone, "gonderim_belirsiz")
        return "belirsiz", "Gonderim dogrulanamadi (resim gitmis olabilir, WhatsApp'tan kontrol et)"
    time.sleep(2.0)
    return "ok", ""


def send_message(page, phone, message, image=None):
    """(durum, aciklama) dondurur. durum: 'ok' | 'gecersiz' | 'hata' | 'belirsiz'.
    image: gonderilecek resmin yolu (varsa mesaj metni resmin basligi olur).
    'belirsiz': gonderim dogrulanamadi, mesaj gitmis olabilir (otomatik tekrar denenmez).
    'gecersiz': WhatsApp numarayi kabul etmedi (kayitli degil / hatali numara).
    'hata': gecici sorun olabilir (internet, sayfa yavasligi, beklenmeyen pencere)."""
    # Resimli gonderimde metin URL ile degil, resmin basligi olarak yazilir
    url = f"{WA_URL}/send?phone={phone}" + ("" if image else f"&text={quote(message)}")
    try:
        page.goto(url)
    except Exception as e:
        return "hata", f"Sohbet acilamadi; mesaj gonderme islemi baslamadi: {e}"
    try:
        # WhatsApp sohbet acilirken gecici "Starting chat / Cancel" penceresi gosterebilir.
        # Bu pencereyi sonuc kabul etme; gercek mesaj kutusu gorunur olana kadar bekle.
        page.wait_for_selector(INPUT_SELECTOR, state="visible", timeout=45000)
    except Exception:
        txt = visible_dialog_text(page)
        low = txt.lower()
        if any(k in low for k in ("invalid", "gecersiz", "geçersiz", "not on whatsapp")):
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return "gecersiz", txt or "WhatsApp bu numarayi gecersiz buldu"
        detail = f" ({txt})" if txt else ""
        return "hata", f"Sohbet 45 sn icinde hazir olmadi{detail}; gecici ag veya WhatsApp sorunu olabilir"
    if image:
        return send_image(page, phone, message, image)
    time.sleep(1.5)
    wait_for_link_preview(page, message)
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
        return "belirsiz", f"Gonderim sonucu dogrulanamadi; tekrar gonderim engellendi: {e}"
    return "ok", ""


def job_exists(job_id):
    with lock:
        return any(j["id"] == job_id for j in jobs)


def run_job(page, job):
    """Tur bazli gorev: listedeki herkese mesaj atar, sonraki tur icin zamanlar."""
    log(f"Gorev basladi: {job['name']} ({len(job['recipients'])} kisi)")
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
            template = (r.get("message") or job["message"] or "").strip()
            message_key = message_fingerprint(template, job.get("image", ""))
            message_id = r.get("message_id") or job.get("message_id", "")
            if previously_sent(r["phone"], message_id, message_key):
                job.setdefault("skipped", []).append({
                    "row": "-", "name": r.get("name", ""), "raw": r["phone"],
                    "reason": "bu mesaj daha once gonderildi", "src": r.get("src"),
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
        try:
            st, note = send_message(page, r["phone"], text, job_image(job))
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
            time.sleep(random.randint(job["min_delay"], job["max_delay"]))

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
        job["current_recipient"] = {
            "row": r.get("row", "-"), "phone": phone, "name": r.get("name", ""),
            "started_at": now.isoformat(timespec="seconds"),
        }
        job["checkpoint_updated_at"] = now.isoformat(timespec="seconds")

        template = (r.get("message") or job["message"] or "").strip()
        message_key = message_fingerprint(template, job.get("image", ""))
        message_id = r.get("message_id") or job.get("message_id", "")
        if previously_sent(phone, message_id, message_key):
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
    try:
        st, note = send_message(page, phone, text, job_image(job))
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
            # gecici hata olabilir: bir kez daha denenecek (durum 'bekliyor' kalir)
            r["attempts"] = r.get("attempts", 0) + 1
            r["error"] = note + " (tekrar denenecek)"
            r["time"] = stamp
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
            if job.get("spread"):
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


def next_due():
    now = datetime.now()
    with lock:
        for j in jobs:
            if j["status"] == "bekliyor" and j["next_run"]:
                if datetime.fromisoformat(j["next_run"]) <= now:
                    j["status"] = "calisiyor"
                    return j
    return None


def launch_whatsapp(p):
    ctx = p.chromium.launch_persistent_context(
        PROFILE_DIR,
        headless=False,
        viewport={"width": 1200, "height": 800},
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(WA_URL)
    state["status"] = "QR kodu okut / giris bekleniyor..."
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


def summarize(j):
    s = {k: v for k, v in j.items() if k not in ("recipients", "skipped", "src_header")}
    n = len(j["recipients"])
    s["n_recipients"] = n
    s["n_skipped"] = len(j.get("skipped", []))
    if j.get("type") == "liste":
        cnt = {"bekliyor": 0, "gonderildi": 0, "gecersiz": 0, "hata": 0, "daha_once_gonderildi": 0}
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


@app.get("/api/logs")
def api_logs():
    try:
        limit = min(max(int(request.args.get("limit", "1000")), 1), 5000)
    except ValueError:
        limit = 1000
    with lock:
        total, items = query_app_logs(request.args.get("q", ""), limit)
    return jsonify({"total": total, "items": items})


@app.get("/api/logs/export")
def api_logs_export():
    query = request.args.get("q", "")
    # CSV export includes all matching log rows, regardless of the UI page limit.
    _, items = query_app_logs(query, 2_147_483_647)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["tarih", "seviye", "olay", "gorev_id", "telefon", "mesaj"])
    for item in items:
        writer.writerow([item["created_at"], item["level"], item["event"],
                         item["job_id"], item["phone"], item["message"]])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="uygulama_loglari.csv"'})


@app.get("/api/history")
def api_history():
    query = request.args.get("q", "").strip().lower()
    try:
        limit = min(max(int(request.args.get("limit", "200")), 1), 2000)
    except ValueError:
        limit = 200
    with lock:
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
    with lock:
        with history_db() as conn:
            items = conn.execute("SELECT phone, name, message, sent_at, job_name, message_id, result, error FROM send_history ORDER BY id DESC").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["telefon", "isim", "mesaj", "tarih", "gorev", "mesaj_id", "sonuc", "aciklama"])
    for h in items:
        w.writerow(list(h))
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="gonderim_gecmisi.csv"'})


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
    d = request.get_json(force=True)
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
    log(f"Gorev eklendi: {job['name']}")
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
        kept = []
        for recipient in recipients:
            if recipient["phone"] in selected:
                kept.append(recipient)
            else:
                skipped.append({"row": recipient.get("row", "-"), "name": recipient.get("name", ""),
                                "raw": recipient["phone"], "reason": "kullanici bu kisiyi atla olarak secti",
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
        limit = max(1, int(form.get("daily_limit") or 30))
        ws = min(max(int(form.get("win_start") or 9), 0), 23)
        we = min(max(int(form.get("win_end") or 21), 1), 24)
        min_d = max(3, int(form.get("min_delay") or 60))
        max_d = max(min_d, int(form.get("max_delay") or 180))
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
    allow_resend = form.get("allow_resend") == "true"
    resend_namespace = uuid.uuid4().hex if allow_resend else ""
    original_message_id = message_id
    if allow_resend:
        message_id = f"resend:{resend_namespace}:{original_message_id or message_fingerprint(message, image)}"
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
        "allow_resend": allow_resend,
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
            if allow_resend:
                r["queue_message_id"] = source_id
                r["message_id"] = f"resend:{resend_namespace}:{source_id or message_fingerprint(template, image)}"
            else:
                r["message_id"] = source_id
        job["recipients"], blocked = phone_blocks(recipients, message, image, bypass_sent=allow_resend)
        job["skipped"] = (job["skipped"] + blocked)[-20000:]
        if not job["recipients"]:
            job["status"] = "tamamlandi"
            job["next_run"] = ""
        jobs.append(job)
        save_jobs()
    total_skipped = len(job["skipped"])
    message_ids = sorted({r.get("message_id") for r in job["recipients"] if r.get("message_id")})
    log(f"Liste eklendi: {name} ({len(job['recipients'])} kisi, {total_skipped} satir atlandi, gunluk {limit})")
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
    return jsonify({
        "items": [{"phone": r["phone"], "name": r.get("name", ""), "row": r.get("row", "-")}
                  for r in recipients],
        "skipped_count": len(skipped),
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
    """phone verilmezse tum 'hata' durumundakiler, verilirse sadece o kisi (gecersiz dahil) tekrar siraya alinir."""
    d = request.get_json(silent=True) or {}
    phone = d.get("phone")
    message_id = d.get("message_id")
    n = 0
    with lock:
        job = find_job(job_id)
        if not job or job.get("type") != "liste":
            return jsonify({"error": "Gorev bulunamadi."}), 404
        for r in job["recipients"]:
            if r["status"] not in ("hata", "gecersiz"):
                continue
            if phone is None and r["status"] != "hata":
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
    return jsonify({"ok": True, "n": n})


@app.post("/api/jobs/<job_id>/recipient/edit")
def api_rec_edit(job_id):
    d = request.get_json(force=True)
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
    return jsonify({"ok": True})


@app.post("/api/jobs/<job_id>/recipient/delete")
def api_rec_delete(job_id):
    d = request.get_json(force=True)
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
    log(f"{job['name']}: {added} yeni kisi eklendi, {len(skipped)} satir atlandi.")
    return jsonify({"ok": True, "info": f"{added} yeni kisi eklendi, {len(skipped)} satir atlandi."})


@app.post("/api/jobs/<job_id>/toggle")
def api_toggle(job_id):
    with lock:
        for j in jobs:
            if j["id"] == job_id:
                if j["status"] in ("bekliyor", "calisiyor"):
                    j["status"] = "duraklatildi"
                elif j["status"] == "duraklatildi":
                    j["status"] = "bekliyor"
                    j["next_run"] = datetime.now().isoformat(timespec="seconds")
                save_jobs()
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
    return jsonify({"ok": True})


HTML = """<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WhatsAppTransporter</title>
 <style>
 :root{font-family:Inter,system-ui,Segoe UI,Arial,sans-serif;color:#172033;background:#f4f6fb;font-synthesis:none}
 *{box-sizing:border-box}
 body{margin:0;background:linear-gradient(135deg,#f4f6fb 0%,#edf2f8 100%);color:#172033}
 .wrap{max-width:1240px;margin:0 auto;padding:28px 24px 56px}
 h1{font-size:25px;letter-spacing:-.04em;margin:0} h2{font-size:17px;letter-spacing:-.02em;margin-top:0}
 .topbar{display:flex;align-items:center;justify-content:space-between;gap:16px;background:linear-gradient(120deg,#111c36,#203b63);color:#fff;border-radius:18px;padding:23px 26px;margin-bottom:18px;box-shadow:0 12px 28px #13294b22}
 .topbar p{margin:5px 0 0;color:#c6d2e5;font-size:13px}
 .stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:18px}
 .stat{background:#fff;border:1px solid #e6eaf1;border-radius:14px;padding:15px 17px;box-shadow:0 4px 14px #21334d08}
 .stat-label{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#738099;font-weight:700}
 .stat-value{display:block;font-size:25px;letter-spacing:-.04em;font-weight:750;margin-top:5px;color:#172033}
 .card{background:#fff;border:1px solid #e6eaf1;border-radius:15px;padding:20px;margin-bottom:16px;box-shadow:0 5px 18px #21334d08}
 label{display:block;font-size:13px;margin:10px 0 4px;color:#444}
  input,textarea,select{width:100%;box-sizing:border-box;padding:10px 12px;border:1px solid #d9dfeb;border-radius:9px;font:inherit;background:#fff;color:#172033;transition:border-color .15s,box-shadow .15s}
 input:focus,textarea:focus,select:focus{outline:none;border-color:#5683d5;box-shadow:0 0 0 3px #5683d522}
 textarea{min-height:90px}
 .row{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
 button{background:#2563eb;color:#fff;border:0;padding:10px 18px;border-radius:9px;font:inherit;font-weight:650;cursor:pointer;margin-top:14px;transition:transform .15s,filter .15s}
 button:hover{filter:brightness(.96);transform:translateY(-1px)}
 button.del{background:#dc2626;padding:5px 10px;margin:0}
 button.pause{background:#d97706;padding:5px 10px;margin:0 4px 0 0}
 button.sm{padding:4px 9px;margin:0 4px 4px 0;font-size:12px}
 button.gray{background:#6b7280}
 button.red{background:#dc2626}
 button.tab{background:#e5e7eb;color:#111;padding:6px 12px;margin:0 6px 6px 0}
 button.tab.on{background:#111;color:#fff}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{text-align:left;padding:11px 10px;border-bottom:1px solid #edf0f5;vertical-align:top}
 th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#78849a;background:#f8f9fc}
 .st{padding:8px 12px;border-radius:999px;background:#fff0c2;color:#855b00;display:inline-block;font-size:13px;font-weight:700}
 .st.ok{background:#d9f7e7;color:#147442}
 pre{background:#111c36;color:#b9d8ff;padding:14px;border-radius:10px;max-height:280px;overflow:auto;font-size:12px;line-height:1.6}
 small{color:#666}
  .note{color:#b45309;font-size:12px;display:block}
  .msgcell{width:220px;max-width:220px;white-space:normal;overflow-wrap:anywhere;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden}
  .history-main{cursor:pointer}
  .history-name{max-width:140px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .history-detail-row td{background:#f9fafb;padding:12px}
  .history-detail-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
  .history-detail-item{min-width:0}
  .history-detail-label{display:block;color:#6b7280;font-size:11px;margin-bottom:3px}
  .history-detail-value{white-space:pre-wrap;overflow-wrap:anywhere}
  .countdown{display:block;color:#b45309;font-size:12px;font-weight:600;margin-top:3px}
  dialog{width:min(640px,calc(100% - 32px));border:0;border-radius:12px;padding:0;box-shadow:0 20px 60px #0004}
  dialog::backdrop{background:#1118}
  .dialog-body{padding:20px}
  .dialog-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:14px}
 .history-main:hover td{background:#f7f9fd}
 .history-main[aria-expanded="true"] td{background:#eef4ff}
 @media(max-width:760px){.wrap{padding:14px 12px 36px}.topbar{align-items:flex-start;flex-direction:column;padding:19px}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.card{padding:15px;overflow-x:auto}.topbar h1{font-size:22px}}
</style></head><body><div class="wrap">
<header class="topbar"><div><h1>WhatsAppTransporter</h1><p>Mesaj kampanyalarini, listeleri ve gonderim sonuclarini yonet</p></div><span id="status" class="st">...</span></header>
<section class="stats" aria-label="Genel durum">
 <div class="stat"><span class="stat-label">Aktif liste</span><strong class="stat-value" id="stat_jobs">0</strong></div>
 <div class="stat"><span class="stat-label">Toplam alici</span><strong class="stat-value" id="stat_recipients">0</strong></div>
 <div class="stat"><span class="stat-label">Gonderildi</span><strong class="stat-value" id="stat_sent">0</strong></div>
 <div class="stat"><span class="stat-label">Kontrol gereken</span><strong class="stat-value" id="stat_attention">0</strong></div>
</section>

<div class="card"><div style="display:flex;justify-content:space-between;align-items:center;gap:8px"><h2 style="margin:0">Mesaj sablonlari</h2><button class="sm gray" onclick="loadSavedMessages()">Yenile</button></div>
 <small>Buraya kaydettigin sablonlar secim listelerinde gorunur. Gonderilen alicilari ve gecmisi buradan inceleyebilirsin.</small>
 <div id="templates_list" style="margin-top:10px"></div>
 <div id="template_recipients" style="display:none;margin-top:12px"></div>
 <dialog id="template_editor">
  <form id="template_editor_form" class="dialog-body">
   <h2 style="margin:0 0 12px">Şablonu düzenle</h2>
   <label for="template_edit_name">Şablon adı</label><input id="template_edit_name" required maxlength="120">
   <label for="template_edit_message">Mesaj</label><textarea id="template_edit_message" required style="min-height:180px"></textarea>
   <label for="template_edit_image_file">Sablon resmi (istege bagli)</label>
   <input id="template_edit_image_file" type="file" accept="image/*" onchange="uploadTemplateImage(this)">
   <div id="template_edit_image_preview"></div>
   <div id="template_edit_error" class="note"></div>
   <div class="dialog-actions"><button type="button" class="sm gray" onclick="document.getElementById('template_editor').close()">Kapat</button><button id="template_edit_save" type="submit" class="sm">Kaydet</button></div>
  </form>
 </dialog>
</div>

<div class="card"><h2>CSV listesi yukle (otomatik, yavas yavas)</h2>
  <label>CSV dosyasi <small>(Google Contacts veya telefon, isim, mesaj sutunlari. Ayni numaraya ayni mesaj tekrar gonderilmez; farkli mesaj gonderilebilir.)</small></label>
 <input id="csv_file" type="file" accept=".csv,.txt" onchange="previewCsv()">
 <div id="csv_preview" style="display:none;margin:10px 0"></div>
 <label>Liste adi (istege bagli)</label><input id="csv_name">
  <label>Sablondan sec</label><div class="row"><select id="csv_saved" onchange="useSavedMessage('csv')"><option value="">Yeni mesaj</option></select><button class="sm gray" onclick="saveCurrentMessage('csv')">Sablon olarak kaydet</button></div>
 <label>Mesaj <small>({isim} yazarsan kisinin adi gelir; CSV'de mesaj sutunu varsa o satir icin o kullanilir)</small></label>
 <textarea id="csv_message" placeholder="Merhaba {isim}, ..."></textarea>
 <div style="margin:6px 0"><small>Resim eklemek icin mesaj kutusuna resmi yapistir (Ctrl+V) ya da sec. Mesaj yazisi resmin alt yazisi olarak gider:</small>
  <input type="file" accept="image/*" style="width:auto" onchange="pickImage('csv',this)">
  <div id="csv_img_prev"></div></div>
 <label style="display:flex;align-items:flex-start;gap:8px;margin:10px 0"><input type="checkbox" id="csv_allow_resend" style="width:auto;margin-top:3px"> Daha once bu mesaji almis kisilere de tekrar gonder (gonderim filtresini atla)</label>
 <small>Isaretlenirse, onceki gonderimler tekrar gonderime engel olmaz. Ayni CSV icindeki tekrarlar ve yarim kalan gorev korumasi devam eder.</small>
 <div class="row">
  <div><label>Gunluk limit (mesaj/gun)</label><input id="csv_limit" type="number" min="1" value="30"></div>
  <div><label>Gonderim baslangic saati</label><input id="csv_ws" type="number" min="0" max="23" value="9"></div>
  <div><label>Gonderim bitis saati</label><input id="csv_we" type="number" min="1" max="24" value="21"></div>
  <div><label>Baslangic zamani <small>(bos = hemen)</small></label><input id="csv_start" type="datetime-local"></div>
 </div>
 <label><input type="checkbox" id="csv_spread" checked style="width:auto"> Mesajlari saat araligina esit yay (onerilir - yavas yavas gider)</label>
 <div class="row">
  <div><label>Yayma kapaliysa: min bekleme (sn)</label><input id="csv_min" type="number" min="3" value="60"></div>
  <div><label>Yayma kapaliysa: max bekleme (sn)</label><input id="csv_max" type="number" min="3" value="180"></div>
 </div>
 <button onclick="addCsv()">Listeyi ekle ve baslat</button>
 <div id="csv_msg" style="margin-top:8px"></div>
</div>

<div class="card"><h2>Gorevler</h2>
 <table><thead><tr><th>Ad</th><th>Kisi</th><th>Durum</th><th>Sonraki</th><th>Ilerleme</th><th>Sonuc</th><th></th></tr></thead>
 <tbody id="jobs"></tbody></table>
</div>

<div class="card"><h2>Gonderim gecmisi</h2>
 <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
  <input id="history_q" placeholder="Telefon, isim, mesaj veya gorev ara" oninput="loadHistory()">
  <button class="sm gray" style="white-space:nowrap" onclick="location.href='/api/history/export'">Gecmisi CSV indir</button>
 </div>
 <small id="history_count"></small>
 <div style="overflow-x:auto"><table><thead><tr><th>Tarih</th><th>Telefon</th><th>Isim</th><th>Sonuc / Detay</th></tr></thead>
 <tbody id="history_rows"></tbody></table></div>
</div>

<div class="card" id="detail" style="display:none">
 <div style="display:flex;justify-content:space-between;align-items:center;gap:10px">
  <h2 id="d_title" style="margin:0"></h2>
  <button class="sm gray" style="margin:0" onclick="closeDetail()">Kapat</button>
 </div>
 <div style="margin:12px 0 4px">
  <button class="sm" id="d_retry" onclick="retryAll()">Hatalilari tekrar dene</button>
  <button class="sm gray" onclick="dl('sorunlu')">Gecersiz/hatali numaralari indir</button>
  <button class="sm gray" onclick="dl('atlanan')">Atlanan satirlari indir</button>
  <button class="sm gray" onclick="dl('all')">Tum listeyi indir</button>
 </div>
 <div style="margin:0 0 8px">
  <button class="sm" onclick="dl('google_tum')">Google Kisiler formatinda indir: guncel liste</button>
  <button class="sm" onclick="dl('google_sorunlu')">Google Kisiler formatinda indir: sadece duzeltilecekler</button>
  <small style="display:block;margin-top:2px">Duzeltilen numaralar +90... biciminde yazilir. Sorunlu kisiler Labels sutununda su etiketlerle isaretlenir: WA Gecersiz Numara, WA Gonderim Hatasi, WA Numara Duzeltildi, WA Tekrar Kisi.</small>
 </div>
 <div id="d_append" style="margin:8px 0">
   <label>Listeye yeni kisi ekle <small>(listede ayni numara + ayni mesaj olanlar atlanir; farkli mesajlar eklenebilir)</small></label>
  <input id="d_file" type="file" accept=".csv,.txt" style="width:auto">
  <button class="sm" onclick="appendCsv()">Yeni kisileri ekle</button>
  <div id="d_msg" style="margin-top:6px"></div>
 </div>
 <div id="d_tabs" style="margin-top:10px"></div>
 <input id="d_q" placeholder="Ara: isim veya numara" oninput="q=this.value;shown=100;renderDetail()" style="margin:6px 0 10px">
 <table><thead><tr id="d_head"></tr></thead><tbody id="d_body"></tbody></table>
</div>

<div class="card"><h2>Uygulama ve gonderim loglari</h2>
 <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
  <input id="log_q" placeholder="Tarih, olay, gorev, telefon veya hata ara" oninput="loadLogs()">
  <button class="sm gray" style="white-space:nowrap" onclick="exportLogs()">Loglari CSV indir</button>
 </div>
 <small id="log_count"></small><pre id="log"></pre>
</div>
</div>
<script>
let openJob=null,D=null,flt='all',q='',shown=100;
const ST={bekliyor:'Bekliyor',gonderildi:'Gonderildi',gecersiz:'Gecersiz numara',hata:'Hata',daha_once_gonderildi:'Daha once gonderildi'};
const COL={gonderildi:'#16a34a',gecersiz:'#dc2626',hata:'#dc2626',bekliyor:'#6b7280',daha_once_gonderildi:'#b45309'};
function cell(tr,text){const td=document.createElement('td');td.textContent=text;tr.appendChild(td);return td}
function mkbtn(label,cls,fn){const b=document.createElement('button');b.textContent=label;b.className='sm '+cls;b.onclick=fn;return b}
function tickCountdowns(){
 const now=Date.now();
 document.querySelectorAll('.countdown[data-target]').forEach(el=>{
  const seconds=Math.max(0,Math.ceil((Number(el.dataset.target)-now)/1000));
  if(!seconds){el.textContent='Siradaki mesaj hazirlaniyor...';return}
  const h=Math.floor(seconds/3600),m=Math.floor((seconds%3600)/60),s=seconds%60;
  el.textContent='Sonraki mesaja kalan: '+(h?String(h).padStart(2,'0')+':':'')+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0');
 });
}
async function post(url,body){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});const j=await r.json();return {ok:r.ok,j:j}}

async function load(){
 try{
  const s=await (await fetch('/api/state')).json();
  const st=document.getElementById('status');
  st.textContent=s.status; st.className='st'+(s.status.startsWith('Hazir')||s.status.startsWith('Gonderiliyor')?' ok':'');
  const activeJobs=s.jobs.filter(j=>j.status!=='tamamlandi').length;
  const recipients=s.jobs.reduce((n,j)=>n+(Number(j.n_recipients)||0),0);
  const sent=s.jobs.reduce((n,j)=>n+(j.counts?Number(j.counts.gonderildi||0):Number(j.sent||0)),0);
  const attention=s.jobs.reduce((n,j)=>n+(j.counts?Number(j.counts.hata||0)+Number(j.counts.gecersiz||0):Number(j.failed||0)),0);
  document.getElementById('stat_jobs').textContent=activeJobs;
  document.getElementById('stat_recipients').textContent=recipients;
  document.getElementById('stat_sent').textContent=sent;
  document.getElementById('stat_attention').textContent=attention;
  const tb=document.getElementById('jobs'); tb.innerHTML='';
  s.jobs.forEach(j=>{
   const tr=document.createElement('tr');
   const nameCell=cell(tr,j.name+(j.image?' [resimli]':''));
   if(j.type==='liste'&&j.status==='bekliyor'&&j.next_run){
    const countdown=document.createElement('small');countdown.className='countdown';
    countdown.dataset.target=String(new Date(j.next_run).getTime());nameCell.appendChild(countdown);
   }
   cell(tr,j.n_recipients);
   const sc=cell(tr,j.status);
   if(j.note){const n=document.createElement('span');n.className='note';n.textContent=j.note;sc.appendChild(n)}
   if(j.resume_notice){const n=document.createElement('span');n.className='note';n.textContent=j.resume_notice;sc.appendChild(n)}
   cell(tr,j.next_run?j.next_run.replace('T',' '):'-');
   const progressCell=cell(tr,j.progress);
   if(j.type==='liste'){
    const checkpoint=[];
    if(j.last_processed){const p=j.last_processed;checkpoint.push('Son islenen: satir '+(p.row||'-')+', '+(p.phone||'')+' ('+({ok:'gonderildi',belirsiz:'belirsiz',hata:'hata',gecersiz:'gecersiz',daha_once_gonderildi:'daha once gonderilmis'}[p.status]||p.status||'')+')')}
    const next=j.current_recipient||j.next_recipient;
    if(next)checkpoint.push((j.current_recipient?'Isleniyor: ':'Siradaki: ')+'satir '+(next.row||'-')+', '+(next.phone||''));
    if(checkpoint.length){const detail=document.createElement('small');detail.className='note';detail.textContent=checkpoint.join(' | ');progressCell.appendChild(document.createElement('br'));progressCell.appendChild(detail)}
   }
   let res=j.sent+(j.failed?(' (hata: '+j.failed+')'):'');
    if(j.counts){const c=j.counts;res=c.gonderildi+' gonderildi';if(c.gecersiz)res+=', '+c.gecersiz+' gecersiz';if(c.hata)res+=', '+c.hata+' hata';if(c.daha_once_gonderildi)res+=', '+c.daha_once_gonderildi+' daha once gonderilmis'}
   if(j.n_skipped)res+=', '+j.n_skipped+' atlanan';
   cell(tr,res);
   const td=document.createElement('td');
   td.appendChild(mkbtn('Detay','gray',()=>openDetail(j.id)));
   if(j.status!=='tamamlandi'){
    const p=document.createElement('button');p.className='pause';
    p.textContent=j.status==='duraklatildi'?'Devam':'Duraklat';
    p.onclick=async()=>{await fetch('/api/jobs/'+j.id+'/toggle',{method:'POST'});load()};
    td.appendChild(p);
   }
   const b=document.createElement('button');
   b.textContent='Sil'; b.className='del'; b.onclick=async()=>{if(confirm('Bu gorev silinsin mi?')){await fetch('/api/jobs/'+j.id,{method:'DELETE'});if(openJob===j.id)closeDetail();load()}};
   td.appendChild(b); tr.appendChild(td); tb.appendChild(tr);
  });
  tickCountdowns();
  loadLogs();
   loadHistory();
  if(openJob)loadDetail();
 }catch(e){}
}

async function loadLogs(){
 try{
  const q=document.getElementById('log_q').value;
  const d=await (await fetch('/api/logs?limit=1000&q='+encodeURIComponent(q))).json();
  document.getElementById('log_count').textContent=d.total+' kayit (son 1000 gosteriliyor)';
  const lg=document.getElementById('log');
  lg.textContent=d.items.slice().reverse().map(x=>`${x.created_at} [${x.level}] ${x.event}${x.job_id?' gorev='+x.job_id:''}${x.phone?' tel='+x.phone:''} ${x.message}`).join('\\n');
  lg.scrollTop=lg.scrollHeight;
 }catch(e){}
}
function exportLogs(){
 const q=document.getElementById('log_q').value;
 location.href='/api/logs/export?q='+encodeURIComponent(q);
}

async function loadHistory(){
 try{
  const q=document.getElementById('history_q').value;
  const d=await (await fetch('/api/history?limit=20&q='+encodeURIComponent(q))).json();
  document.getElementById('history_count').textContent='Toplam '+d.total+' kayit · son 20 gosteriliyor';
  const body=document.getElementById('history_rows');
  const previousOpen=body.querySelector('.history-main[aria-expanded="true"]');
  const openHistoryId=previousOpen?previousOpen.dataset.historyId:'';
  body.innerHTML='';
  d.items.forEach(h=>{
   const tr=document.createElement('tr');
   tr.className='history-main';tr.tabIndex=0;tr.dataset.historyId=String(h.id||'');tr.setAttribute('aria-expanded','false');
   cell(tr,(h.sent_at||'').replace('T',' '));cell(tr,h.phone||'');
   const person=cell(tr,h.name||'');person.className='history-name';person.title=h.name||'';
   const result={ok:'Gonderildi',belirsiz:'Belirsiz',hata:'Hata',gecersiz:'Gecersiz'}[h.result]||h.result||'';
   const status=cell(tr,result+'  +');
   if(h.result==='ok')status.style.color='#16a34a';else status.style.color='#b45309';
   const detailRow=document.createElement('tr');detailRow.className='history-detail-row';detailRow.hidden=true;
   const detailCell=document.createElement('td');detailCell.colSpan=4;
   const grid=document.createElement('div');grid.className='history-detail-grid';
   for(const [label,value] of [['Mesaj',h.message],['Gorev',h.job_name],['Mesaj ID',h.message_id],['Aciklama',h.error]]){
    if(!value)continue;
    const item=document.createElement('div');item.className='history-detail-item';
    const caption=document.createElement('span');caption.className='history-detail-label';caption.textContent=label;
    const content=document.createElement('div');content.className='history-detail-value';content.textContent=value;
    item.append(caption,content);grid.appendChild(item);
   }
   detailCell.appendChild(grid);detailRow.appendChild(detailCell);
   const toggle=()=>{
    const willOpen=detailRow.hidden;
    body.querySelectorAll('.history-detail-row').forEach(row=>row.hidden=true);
    body.querySelectorAll('.history-main').forEach(row=>row.setAttribute('aria-expanded','false'));
    detailRow.hidden=!willOpen;tr.setAttribute('aria-expanded',String(willOpen));
    status.textContent=result+(willOpen?'  -':'  +');
   };
   tr.addEventListener('click',toggle);
   tr.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();toggle()}});
   body.append(tr,detailRow);
   if(openHistoryId && openHistoryId===tr.dataset.historyId){
    detailRow.hidden=false;tr.setAttribute('aria-expanded','true');status.textContent=result+'  -';
   }
  });
  if(!d.items.length){const tr=document.createElement('tr');const td=cell(tr,'Gonderim gecmisi bos.');td.colSpan=4;body.appendChild(tr)}
 }catch(e){}
}

async function openDetail(id){
 openJob=id;flt='all';q='';shown=100;
 document.getElementById('d_q').value='';
 document.getElementById('d_msg').textContent='';
 document.getElementById('detail').style.display='block';
 await loadDetail();
 document.getElementById('detail').scrollIntoView({behavior:'smooth'});
}
function closeDetail(){openJob=null;D=null;document.getElementById('detail').style.display='none'}
async function loadDetail(){
 if(!openJob)return;
 const r=await fetch('/api/jobs/'+openJob+'/recipients');
 if(!r.ok){closeDetail();return}
 D=await r.json();
 renderDetail();
}
function setHead(cols){const h=document.getElementById('d_head');h.innerHTML='';cols.forEach(c=>{const th=document.createElement('th');th.textContent=c;h.appendChild(th)})}
function more(n,body,span){
 if(n>shown){
  const tr=document.createElement('tr');const td=document.createElement('td');td.colSpan=span;
  td.appendChild(mkbtn('Daha fazla goster ('+(n-shown)+' kayit daha)','gray',()=>{shown+=200;renderDetail()}));
  tr.appendChild(td);body.appendChild(tr);
 }
}
function renderDetail(){
 if(!D)return;
 const isList=D.type==='liste';
 document.getElementById('d_title').textContent=D.name+' - liste detayi';
 document.getElementById('d_append').style.display=isList?'block':'none';
 document.getElementById('d_retry').style.display=isList?'inline-block':'none';
 const rec=D.recipients, sk=D.skipped||[];
  const c={all:rec.length,bekliyor:0,gonderildi:0,gecersiz:0,hata:0,daha_once_gonderildi:0,atlanan:sk.length};
 rec.forEach(r=>{if(c[r.status]!==undefined)c[r.status]++});
  const tabs=[['all','Tumu'],['bekliyor','Bekliyor'],['gonderildi','Gonderildi'],['daha_once_gonderildi','Daha once gonderildi'],['gecersiz','Gecersiz numara'],['hata','Hata'],['atlanan','Atlanan satirlar']];
 const tb=document.getElementById('d_tabs'); tb.innerHTML='';
 tabs.forEach(t=>{
  const b=document.createElement('button');b.className='tab'+(flt===t[0]?' on':'');
  b.textContent=t[1]+' ('+c[t[0]]+')';
  b.onclick=()=>{flt=t[0];shown=100;renderDetail()};
  tb.appendChild(b);
 });
 const body=document.getElementById('d_body'); body.innerHTML='';
 const needle=q.trim().toLowerCase();
 if(flt==='atlanan'){
  setHead(['Satir','Isim','Girilen deger','Neden']);
  const rows=sk.filter(s=>!needle||((s.name||'')+' '+(s.raw||'')).toLowerCase().includes(needle));
  rows.slice(0,shown).forEach(s=>{const tr=document.createElement('tr');cell(tr,s.row);cell(tr,s.name);cell(tr,s.raw);cell(tr,s.reason);body.appendChild(tr)});
  if(!rows.length){const tr=document.createElement('tr');const td=cell(tr,'Atlanan satir yok.');td.colSpan=4;body.appendChild(tr)}
  more(rows.length,body,4);
  return;
 }
 setHead(['Telefon','Isim','Durum','Aciklama','Zaman','']);
 const rows=rec.filter(r=>(flt==='all'||r.status===flt)&&(!needle||(r.phone+' '+(r.name||'')).toLowerCase().includes(needle)));
 rows.slice(0,shown).forEach(r=>{
  const tr=document.createElement('tr');
  cell(tr,r.phone);cell(tr,r.name);
  const sc=cell(tr,ST[r.status]||r.status);sc.style.color=COL[r.status]||'#111';
  cell(tr,r.error||'');
  cell(tr,r.time?r.time.replace('T',' '):'');
  const td=document.createElement('td');
  if(isList){
    td.appendChild(mkbtn('Duzelt','gray',()=>editRec(r.phone,r.message_id)));
    if(r.status==='hata'||r.status==='gecersiz')td.appendChild(mkbtn('Tekrar dene','',()=>retryRec(r.phone,r.message_id)));
    td.appendChild(mkbtn('Sil','red',()=>delRec(r.phone,r.message_id)));
  }
  tr.appendChild(td);body.appendChild(tr);
 });
 if(!rows.length){const tr=document.createElement('tr');const td=cell(tr,'Bu filtrede kayit yok.');td.colSpan=6;body.appendChild(tr)}
 more(rows.length,body,6);
}
function afterChange(){loadDetail();load()}
async function retryRec(phone,message_id){await post('/api/jobs/'+openJob+'/retry',{phone:phone,message_id:message_id});afterChange()}
async function retryAll(){const x=await post('/api/jobs/'+openJob+'/retry',{});alert(x.ok?(x.j.n+' kisi yeniden siraya alindi.'):x.j.error);afterChange()}
async function editRec(phone,message_id){
 const v=prompt('Yeni numara (ulke koduyla, ornek 905551234567):',phone);
 if(!v||v===phone)return;
  const x=await post('/api/jobs/'+openJob+'/recipient/edit',{phone:phone,message_id:message_id,new_phone:v});
 if(!x.ok)alert(x.j.error);
 afterChange();
}
async function delRec(phone,message_id){if(!confirm(phone+' listeden silinsin mi?'))return;await post('/api/jobs/'+openJob+'/recipient/delete',{phone:phone,message_id:message_id});afterChange()}
function dl(which){window.location='/api/jobs/'+openJob+'/export?which='+which}
async function appendCsv(){
 const m=document.getElementById('d_msg');
 const f=document.getElementById('d_file').files[0];
 if(!f){m.style.color='#dc2626';m.textContent='Once bir CSV dosyasi sec.';return}
 const fd=new FormData();fd.append('file',f);
 const r=await fetch('/api/jobs/'+openJob+'/append',{method:'POST',body:fd});
 const j=await r.json();
 m.style.color=r.ok?'#16a34a':'#dc2626';m.textContent=j.error||j.info||'';
 if(r.ok){document.getElementById('d_file').value='';afterChange()}
}

const IMG={csv:''};
const SELECTED_MESSAGE={csv:''};
const SELECTED_TEMPLATE={csv:''};
let SAVED_MESSAGES=[];
async function loadSavedMessages(){
 try{
  const d=await (await fetch('/api/templates')).json();SAVED_MESSAGES=d.items||[];
  for(const kind of ['csv']){
   const sel=document.getElementById(kind+'_saved'),old=SELECTED_TEMPLATE[kind];
   sel.innerHTML='<option value="">Yeni mesaj</option>';
   SAVED_MESSAGES.forEach(m=>{
    const o=document.createElement('option');o.value=m.id;
    const preview=(m.message||'').replace(/\\s+/g,' ').trim();
    const short=preview.length>48?preview.slice(0,48)+'...':preview;
    o.textContent=(m.name||'Sablon')+' - '+short;o.title=preview;sel.appendChild(o);
   });
   if(SAVED_MESSAGES.some(m=>m.id===old))sel.value=old;
  }
  renderTemplates();
 }catch(e){}
}
function renderTemplates(){
 const root=document.getElementById('templates_list');root.innerHTML='';
 if(!SAVED_MESSAGES.length){root.textContent='Kayitli sablon yok. Bir mesaj yazip "Sablon olarak kaydet" dugmesini kullan.';return}
 const table=document.createElement('table');
 const head=document.createElement('thead'),hr=document.createElement('tr');
 ['Sablon','Gonderilen kisi','Islemler'].forEach(t=>{const th=document.createElement('th');th.textContent=t;hr.appendChild(th)});
 head.appendChild(hr);table.appendChild(head);
 const body=document.createElement('tbody');
 SAVED_MESSAGES.forEach(m=>{
  const tr=document.createElement('tr');
  const info=document.createElement('td');info.textContent=m.name+' - '+(m.message||'').replace(/\\s+/g,' ').slice(0,64);info.title=m.message||'';tr.appendChild(info);
  const count=document.createElement('td');count.textContent=String(m.sent_recipients||0);tr.appendChild(count);
  const actions=document.createElement('td');
  actions.appendChild(mkbtn('Alicilari goster','gray',()=>showTemplateRecipients(m.id)));
  actions.appendChild(mkbtn('Duzenle','gray',()=>editTemplate(m.id)));
  actions.appendChild(mkbtn('Sil','red',()=>deleteTemplate(m.id)));
  tr.appendChild(actions);body.appendChild(tr);
 });
 table.appendChild(body);root.appendChild(table);
}
function useSavedMessage(kind){
 const id=document.getElementById(kind+'_saved').value;SELECTED_TEMPLATE[kind]=id;
 const item=SAVED_MESSAGES.find(m=>m.id===id);
 SELECTED_MESSAGE[kind]=item?item.message_id:'';
 document.getElementById(kind==='csv'?'csv_message':'message').value=item?item.message:'';
 IMG[kind]=item?(item.image||''):'';showPrev(kind,IMG[kind]?'/images/'+encodeURIComponent(IMG[kind]):'');
}
async function saveCurrentMessage(kind){
 const text=document.getElementById(kind==='csv'?'csv_message':'message').value.trim();
 if(!text){alert('Once mesaj metnini yaz.');return}
 const name=prompt('Sablon adi:', 'Sablon '+new Date().toLocaleDateString());
 if(name===null)return;
 const r=await fetch('/api/templates',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,message:text,image:IMG[kind]||''})});
 const j=await r.json();if(!r.ok){alert(j.error||'Sablon kaydedilemedi.');return}
 await loadSavedMessages();SELECTED_TEMPLATE[kind]=j.item.id;SELECTED_MESSAGE[kind]=j.item.message_id;
 document.getElementById(kind+'_saved').value=j.item.id;
 document.getElementById(kind==='csv'?'csv_message':'message').value=j.item.message;
}
let EDITING_TEMPLATE_ID='';
let EDITING_TEMPLATE_IMAGE='';
function editTemplate(id){
 const item=SAVED_MESSAGES.find(m=>m.id===id);if(!item)return;
 EDITING_TEMPLATE_ID=id;
 EDITING_TEMPLATE_IMAGE=item.image||'';
 document.getElementById('template_edit_name').value=item.name||'';
 document.getElementById('template_edit_message').value=item.message||'';
 document.getElementById('template_edit_error').textContent='';
 document.getElementById('template_edit_image_file').value='';renderTemplateImagePreview();
 document.getElementById('template_editor').showModal();
}
function renderTemplateImagePreview(){
 const box=document.getElementById('template_edit_image_preview');box.innerHTML='';
 if(!EDITING_TEMPLATE_IMAGE)return;
 const img=document.createElement('img');img.src='/images/'+encodeURIComponent(EDITING_TEMPLATE_IMAGE);img.style.cssText='max-width:220px;max-height:150px;display:block;margin:8px 0';box.appendChild(img);
 box.appendChild(mkbtn('Resmi kaldir','gray',()=>{EDITING_TEMPLATE_IMAGE='';renderTemplateImagePreview()}));
}
async function uploadTemplateImage(input){
 if(!input.files[0])return;
 const fd=new FormData();fd.append('file',input.files[0],input.files[0].name||'resim.png');
 const r=await fetch('/api/upload',{method:'POST',body:fd});const j=await r.json();
 if(!r.ok){document.getElementById('template_edit_error').textContent=j.error||'Resim yuklenemedi.';input.value='';return}
 EDITING_TEMPLATE_IMAGE=j.name;document.getElementById('template_edit_error').textContent='';renderTemplateImagePreview();input.value='';
}
document.getElementById('template_editor_form').addEventListener('submit',async e=>{
 e.preventDefault();if(!EDITING_TEMPLATE_ID)return;
 const name=document.getElementById('template_edit_name').value.trim();
 const message=document.getElementById('template_edit_message').value.trim();
 const save=document.getElementById('template_edit_save'),error=document.getElementById('template_edit_error');
 if(!name||!message){error.textContent='Şablon adı ve mesaj gerekli.';return}
 save.disabled=true;error.textContent='';
 try{
  const r=await fetch('/api/templates/'+EDITING_TEMPLATE_ID,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,message:message,image:EDITING_TEMPLATE_IMAGE})});
  const j=await r.json();if(!r.ok){error.textContent=j.error||'Şablon güncellenemedi.';return}
  const id=EDITING_TEMPLATE_ID;
  for(const kind of ['csv'])if(SELECTED_TEMPLATE[kind]===id){SELECTED_MESSAGE[kind]=j.item.message_id;document.getElementById('csv_message').value=j.item.message;IMG[kind]=j.item.image||'';showPrev(kind,IMG[kind]?'/images/'+encodeURIComponent(IMG[kind]):'');}
  document.getElementById('template_editor').close();EDITING_TEMPLATE_ID='';
  await loadSavedMessages();
 }catch(err){error.textContent='Kaydetme hatası: '+err.message}
 finally{save.disabled=false}
});
async function deleteTemplate(id){
 const item=SAVED_MESSAGES.find(m=>m.id===id);if(!item||!confirm('"'+item.name+'" sablonu silinsin mi? Gecmis kayitlari korunur.'))return;
 const r=await fetch('/api/templates/'+id,{method:'DELETE'});const j=await r.json();
 if(!r.ok){alert(j.error||'Sablon silinemedi.');return}
 for(const kind of ['csv'])if(SELECTED_TEMPLATE[kind]===id){SELECTED_TEMPLATE[kind]='';SELECTED_MESSAGE[kind]='';document.getElementById(kind+'_saved').value='';}
 await loadSavedMessages();
}
async function showTemplateRecipients(id){
 const box=document.getElementById('template_recipients');box.style.display='block';box.textContent='Yukleniyor...';
 const r=await fetch('/api/templates/'+id+'/recipients');const d=await r.json();
 if(!r.ok){box.textContent=d.error||'Alicilar yuklenemedi.';return}
 box.innerHTML='';const title=document.createElement('strong');title.textContent=d.name+' - gonderim gecmisi ('+d.items.length+')';box.appendChild(title);
 box.appendChild(mkbtn('Kapat','gray',()=>{box.style.display='none';box.innerHTML=''}));
 if(!d.items.length){const p=document.createElement('p');p.textContent='Bu sablonla henuz gonderim yapilmamis.';box.appendChild(p);return}
 const table=document.createElement('table');table.style.marginTop='8px';
 const hr=document.createElement('tr');['Tarih','Telefon','Isim','Sonuc','Gorev'].forEach(t=>{const th=document.createElement('th');th.textContent=t;hr.appendChild(th)});
 const head=document.createElement('thead');head.appendChild(hr);table.appendChild(head);
 const body=document.createElement('tbody');d.items.forEach(x=>{const tr=document.createElement('tr');
  [String(x.sent_at||'').replace('T',' '),x.phone||'',x.name||'',({ok:'Gonderildi',belirsiz:'Belirsiz',hata:'Hata',gecersiz:'Gecersiz'}[x.result]||x.result||''),x.job_name||''].forEach(v=>{const td=document.createElement('td');td.textContent=v;tr.appendChild(td)});body.appendChild(tr);
 });table.appendChild(body);const scroll=document.createElement('div');scroll.style.cssText='overflow:auto;max-height:320px';scroll.appendChild(table);box.appendChild(scroll);
}
for(const kind of ['csv']){
 const field=document.getElementById('csv_message');
 field.addEventListener('input',()=>{
  const selected=SAVED_MESSAGES.find(m=>m.id===SELECTED_TEMPLATE[kind]);
  if(selected&&field.value.trim()!==selected.message){SELECTED_TEMPLATE[kind]='';SELECTED_MESSAGE[kind]='';document.getElementById(kind+'_saved').value='';}
 });
}
async function uploadImage(kind,file){
 const prev=document.getElementById(kind+'_img_prev');
 const fd=new FormData();fd.append('file',file,file.name||'resim.png');
 const r=await fetch('/api/upload',{method:'POST',body:fd});
 const j=await r.json();
 if(!r.ok){IMG[kind]='';prev.textContent=j.error;prev.style.color='#dc2626';return}
 IMG[kind]=j.name;showPrev(kind,j.url);
}
function showPrev(kind,url){
 const prev=document.getElementById(kind+'_img_prev');prev.innerHTML='';prev.style.color='';
 if(!url)return;
 const img=document.createElement('img');img.src=url;
 img.style.cssText='max-height:120px;max-width:100%;border-radius:6px;display:block;margin:6px 0';
 prev.appendChild(img);
 prev.appendChild(mkbtn('Resmi kaldir','red',()=>{IMG[kind]='';showPrev(kind,'')}));
}
function pickImage(kind,inp){if(inp.files[0])uploadImage(kind,inp.files[0]);inp.value=''}
function hookPaste(id,kind){
 document.getElementById(id).addEventListener('paste',e=>{
  const items=(e.clipboardData&&e.clipboardData.items)||[];
  for(const it of items){
   if(it.type&&it.type.startsWith('image/')){e.preventDefault();uploadImage(kind,it.getAsFile());return}
  }
 });
}
let CSV_PREVIEW=[],CSV_SEND_PHONES=new Set(),CSV_PREVIEW_KEY='';
function csvFileKey(file){return file?`${file.name}:${file.size}:${file.lastModified}`:''}
function updateCsvPreviewCount(){
 const count=document.getElementById('csv_preview_count');if(count)count.textContent=`${CSV_SEND_PHONES.size} / ${CSV_PREVIEW.length} kisi gonderilecek`;
}
function renderCsvPreview(skippedCount=0){
 const root=document.getElementById('csv_preview');root.innerHTML='';root.style.display='block';
 const heading=document.createElement('div');heading.style.cssText='display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:8px';
 const count=document.createElement('strong');count.id='csv_preview_count';heading.appendChild(count);
 heading.appendChild(mkbtn('Tumunu sec','gray',()=>{CSV_SEND_PHONES=new Set(CSV_PREVIEW.map(x=>x.phone));renderCsvPreview(skippedCount)}));
 heading.appendChild(mkbtn('Tumunu atla','gray',()=>{CSV_SEND_PHONES.clear();renderCsvPreview(skippedCount)}));
 root.appendChild(heading);updateCsvPreviewCount();
 if(skippedCount){const note=document.createElement('small');note.style.display='block';note.textContent=`CSV'de gecersiz veya dosya ici tekrar olan ${skippedCount} satir listelenmedi.`;root.appendChild(note)}
 if(!CSV_PREVIEW.length){const empty=document.createElement('p');empty.textContent='CSV icinde gecerli kisi bulunamadi.';root.appendChild(empty);return}
 const scroll=document.createElement('div');scroll.style.cssText='max-height:260px;overflow:auto;border:1px solid #ddd;border-radius:6px;margin-top:6px';
 const table=document.createElement('table'),head=document.createElement('thead'),hr=document.createElement('tr');
 ['Gonder','Isim','Telefon','CSV satiri'].forEach(label=>{const th=document.createElement('th');th.textContent=label;hr.appendChild(th)});
 head.appendChild(hr);table.appendChild(head);const body=document.createElement('tbody');
 CSV_PREVIEW.forEach(person=>{
  const tr=document.createElement('tr'),cell=document.createElement('td'),check=document.createElement('input');
  check.type='checkbox';check.checked=CSV_SEND_PHONES.has(person.phone);check.style.width='auto';check.setAttribute('aria-label',`${person.phone} numarasina gonder`);
  check.addEventListener('change',()=>{if(check.checked)CSV_SEND_PHONES.add(person.phone);else CSV_SEND_PHONES.delete(person.phone);updateCsvPreviewCount()});
  cell.appendChild(check);tr.appendChild(cell);
  for(const value of [person.name||'',person.phone||'',String(person.row??'')]){const td=document.createElement('td');td.textContent=value;tr.appendChild(td)}
  body.appendChild(tr);
 });
 table.appendChild(body);scroll.appendChild(table);root.appendChild(scroll);
}
async function previewCsv(){
 const file=document.getElementById('csv_file').files[0],root=document.getElementById('csv_preview');
 CSV_PREVIEW=[];CSV_SEND_PHONES.clear();CSV_PREVIEW_KEY='';
 if(!file){root.style.display='none';root.innerHTML='';return}
 root.style.display='block';root.textContent='CSV kisileri okunuyor...';
 const fd=new FormData();fd.append('file',file);
 try{
  const response=await fetch('/api/csv/preview',{method:'POST',body:fd}),data=await response.json();
  if(!response.ok){root.textContent=data.error||'CSV okunamadi.';return}
  CSV_PREVIEW=data.items||[];CSV_SEND_PHONES=new Set(CSV_PREVIEW.map(x=>x.phone));CSV_PREVIEW_KEY=csvFileKey(file);
  renderCsvPreview(data.skipped_count||0);
 }catch(error){root.textContent='CSV onizleme hatasi: '+error.message}
}
async function addCsv(){
 const msg=document.getElementById('csv_msg');
 const f=document.getElementById('csv_file').files[0];
 if(!f){msg.style.color='#dc2626';msg.textContent='Once bir CSV dosyasi sec.';return}
 if(CSV_PREVIEW_KEY!==csvFileKey(f)){msg.style.color='#dc2626';msg.textContent='CSV onizlemesinin tamamlanmasini bekle.';return}
 if(!CSV_SEND_PHONES.size){msg.style.color='#dc2626';msg.textContent='Gonderilecek en az bir kisi sec.';return}
 const fd=new FormData();
 fd.append('file',f);
 fd.append('name',document.getElementById('csv_name').value);
 fd.append('message',document.getElementById('csv_message').value);
  fd.append('message_id',SELECTED_MESSAGE.csv);
 fd.append('template_id',SELECTED_TEMPLATE.csv);
 fd.append('image',IMG.csv);
 fd.append('allow_resend',document.getElementById('csv_allow_resend').checked?'true':'false');
 fd.append('send_phones',JSON.stringify([...CSV_SEND_PHONES]));
 fd.append('daily_limit',document.getElementById('csv_limit').value);
 fd.append('win_start',document.getElementById('csv_ws').value);
 fd.append('win_end',document.getElementById('csv_we').value);
 fd.append('start',document.getElementById('csv_start').value);
 fd.append('spread',document.getElementById('csv_spread').checked?'true':'false');
 fd.append('min_delay',document.getElementById('csv_min').value);
 fd.append('max_delay',document.getElementById('csv_max').value);
 const r=await fetch('/api/jobs/csv',{method:'POST',body:fd});
 const j=await r.json();
 msg.style.color=r.ok?'#16a34a':'#dc2626';
 msg.textContent=j.error||j.info||'';
  if(r.ok){document.getElementById('csv_file').value='';document.getElementById('csv_preview').style.display='none';document.getElementById('csv_preview').innerHTML='';CSV_PREVIEW=[];CSV_SEND_PHONES.clear();CSV_PREVIEW_KEY='';document.getElementById('csv_message').value='';document.getElementById('csv_saved').value='';document.getElementById('csv_allow_resend').checked=false;SELECTED_MESSAGE.csv='';SELECTED_TEMPLATE.csv='';document.getElementById('csv_name').value='';IMG.csv='';showPrev('csv','');loadSavedMessages();load()}
}
hookPaste('csv_message','csv');
loadSavedMessages();
load(); setInterval(load,4000);setInterval(tickCountdowns,1000);
</script></body></html>
"""


if __name__ == "__main__":
    load_jobs()
    load_history()
    threading.Thread(target=worker, daemon=True).start()
    threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:5001")).start()
    app.run(host="127.0.0.1", port=5001, threaded=True, use_reloader=False)
