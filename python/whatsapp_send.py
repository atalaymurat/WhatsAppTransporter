"""
WhatsApp Web otomasyonu (Playwright)

Kurulum:
    pip install playwright
    playwright install chromium

Ilk giris (QR kodu bir kez okutulur, oturum ./wa_profile klasorunde saklanir):
    python whatsapp_send.py --login

Tek mesaj:
    python whatsapp_send.py --phone 905551234567 --message "Merhaba!"

Listeden mesaj (CSV: phone,message basligiyla):
    python whatsapp_send.py --csv kisiler.csv

Telefon numarasi ulke koduyla, + ve bosluk olmadan yazilir (Turkiye icin 90...).
"""

import argparse
import csv
import random
import sys
import time
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

PROFILE_DIR = str(Path(__file__).parent / "wa_profile")
WA_URL = "https://web.whatsapp.com"

# Mesaj yazma kutusu (sohbet acikken sayfanin altinda)
INPUT_SELECTOR = 'footer div[contenteditable="true"]'
# Sohbet listesi (giris yapildi demek)
CHATLIST_SELECTOR = '#pane-side'


def open_context(p):
    return p.chromium.launch_persistent_context(
        PROFILE_DIR,
        headless=False,  # WhatsApp Web headless modda sorun cikarabilir
        viewport={"width": 1280, "height": 850},
        args=["--disable-blink-features=AutomationControlled"],
    )


def wait_for_login(page, timeout_s=180):
    page.goto(WA_URL)
    print("WhatsApp Web aciliyor... QR kod cikarsa telefonundan okut.")
    try:
        page.wait_for_selector(CHATLIST_SELECTOR, timeout=timeout_s * 1000)
    except PWTimeout:
        print("Giris yapilamadi (zaman asimi).")
        sys.exit(1)
    print("Giris tamam.")


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


def send_message(page, phone, message):
    phone = "".join(ch for ch in phone if ch.isdigit())
    url = f"{WA_URL}/send?phone={phone}&text={quote(message)}"
    page.goto(url)
    try:
        box = page.wait_for_selector(INPUT_SELECTOR, timeout=45000)
    except PWTimeout:
        print(f"[HATA] {phone}: sohbet acilamadi (numara gecersiz veya WhatsApp'ta yok olabilir).")
        return False
    time.sleep(1.5)  # metnin kutuya yerlesmesini bekle
    wait_for_link_preview(page, message)
    box.press("Enter")
    time.sleep(2.5)  # gonderimin tamamlanmasini bekle
    print(f"[OK] {phone}: gonderildi")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="Sadece giris yap / QR okut")
    ap.add_argument("--phone", help="Ulke kodlu numara, ornek 905551234567")
    ap.add_argument("--message", help="Gonderilecek mesaj")
    ap.add_argument("--csv", help="phone,message sutunlu CSV dosyasi")
    ap.add_argument("--min-delay", type=int, default=8, help="Mesajlar arasi min bekleme (sn)")
    ap.add_argument("--max-delay", type=int, default=15, help="Mesajlar arasi max bekleme (sn)")
    args = ap.parse_args()

    jobs = []
    if args.phone and args.message:
        jobs.append((args.phone, args.message))
    if args.csv:
        with open(args.csv, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                jobs.append((row["phone"], row["message"]))

    if not args.login and not jobs:
        ap.print_help()
        sys.exit(1)

    with sync_playwright() as p:
        ctx = open_context(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        wait_for_login(page)

        if args.login and not jobs:
            print("Oturum kaydedildi. Artik mesaj gonderebilirsin.")
            time.sleep(3)
            ctx.close()
            return

        for i, (phone, message) in enumerate(jobs):
            send_message(page, phone, message)
            if i < len(jobs) - 1:
                wait = random.randint(args.min_delay, args.max_delay)
                print(f"{wait} sn bekleniyor...")
                time.sleep(wait)

        ctx.close()


if __name__ == "__main__":
    main()
