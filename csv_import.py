"""CSV parsing and phone normalization for WhatsAppTransporter."""

import csv
import io


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


def parse_csv(raw_bytes, message_fingerprint):
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
