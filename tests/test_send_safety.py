import tempfile
import sqlite3
import unittest
from io import BytesIO
import json
from pathlib import Path
from unittest.mock import patch

import wa_panel


class SendSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        wa_panel.HISTORY_DB = root / "history.sqlite3"
        wa_panel.JOBS_FILE = root / "jobs.json"
        wa_panel.IMAGES_DIR = root / "images"
        wa_panel.jobs = []
        wa_panel.sent_message_map = {}
        wa_panel.load_history()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_application_logs_persist_with_send_context_and_are_searchable(self):
        wa_panel.log("Gonderildi: 905551234567", level="INFO", event="send_result",
                     job_id="job-log-1", phone="905551234567")
        wa_panel.state["log"] = []  # simulate loss of the in-memory display buffer

        response = wa_panel.app.test_client().get("/api/logs?q=905551234567")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["items"][0]["event"], "send_result")
        self.assertEqual(body["items"][0]["job_id"], "job-log-1")
        self.assertEqual(body["items"][0]["phone"], "905551234567")

    def test_application_log_csv_export_contains_record(self):
        wa_panel.log("Starting chat Cancel", level="WARNING", event="send_result",
                     job_id="job-log-2", phone="905551234568")

        response = wa_panel.app.test_client().get("/api/logs/export?q=Starting%20chat")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.content_type)
        self.assertIn("job-log-2".encode(), response.data)
        self.assertIn("Starting chat Cancel".encode(), response.data)

    def test_reserved_send_survives_reload_and_blocks_same_phone_message(self):
        job = {"id": "job-1", "name": "test", "image": ""}
        recipient = {"phone": "905551234567", "name": "Test"}
        key = wa_panel.message_fingerprint("Merhaba")
        message_id = "template-message-id"

        wa_panel.record_attempt(job, recipient, "Merhaba", "belirsiz", "sending", key, message_id)
        wa_panel.sent_message_map = {}
        wa_panel.load_history()

        self.assertTrue(wa_panel.previously_sent(recipient["phone"], message_id, key))
        with self.assertRaises(sqlite3.IntegrityError):
            wa_panel.record_attempt(job, recipient, "Merhaba", "belirsiz", "second", key, message_id)

    def test_unexpected_send_exception_is_uncertain_and_not_retried(self):
        recipient = {
            "phone": "905551234567", "name": "Test", "message": "Merhaba",
            "message_id": "template-message-id", "status": "bekliyor",
        }
        job = {
            "id": "job-2", "type": "liste", "name": "test", "recipients": [recipient],
            "message": "Merhaba", "message_id": recipient["message_id"], "template_id": "template-id",
            "image": "", "status": "calisiyor", "win_start": 0, "win_end": 24,
            "daily_limit": 10, "sent_today": 0, "day": "", "min_delay": 3,
            "max_delay": 3, "spread": False, "failed": 0, "sent": 0,
        }
        wa_panel.jobs = [job]

        with patch.object(wa_panel, "send_message", side_effect=RuntimeError("page froze after send")) as sender:
            wa_panel.run_list_step(object(), job)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(recipient["status"], "hata")
        self.assertTrue(wa_panel.previously_sent(recipient["phone"], recipient["message_id"],
                                                 wa_panel.message_fingerprint("Merhaba")))

        # Simulate a crash before the job JSON was updated, then restart.
        recipient["status"] = "bekliyor"
        job["status"] = "bekliyor"
        wa_panel.load_history()
        with patch.object(wa_panel, "send_message") as sender:
            wa_panel.run_list_step(object(), job)
        sender.assert_not_called()
        self.assertEqual(recipient["status"], "daha_once_gonderildi")

    def test_resumed_manual_job_skips_recipient_already_in_history(self):
        recipient = {
            "phone": "905551234567", "name": "Test", "message_id": "template-message-id",
        }
        key = wa_panel.message_fingerprint("Merhaba")
        job = {"id": "job-3", "name": "manual", "image": ""}
        attempt_id = wa_panel.record_attempt(job, recipient, "Merhaba", "belirsiz", "sending", key,
                                             recipient["message_id"])
        wa_panel.finish_attempt(attempt_id, "ok")
        recipient["status"] = "bekliyor"
        resumed_job = {
            "id": "job-3", "type": "tur", "name": "manual", "recipients": [recipient],
            "message": "Merhaba", "message_id": recipient["message_id"], "image": "",
            "status": "calisiyor", "recipient_index": 0, "remaining": 1, "total": 1,
            "min_delay": 3, "max_delay": 3, "interval_min": 1, "sent": 0, "failed": 0,
        }
        wa_panel.jobs = [resumed_job]

        with patch.object(wa_panel, "send_message") as sender, patch.object(wa_panel, "save_jobs"):
            wa_panel.run_job(object(), resumed_job)

        sender.assert_not_called()
        self.assertEqual(resumed_job["remaining"], 0)
        self.assertTrue(resumed_job["skipped"])

    def test_text_send_waits_for_compose_input_past_transient_starting_chat_dialog(self):
        class Locator:
            def __init__(self, texts=None):
                self.texts = texts or []
                self.press_count = 0

            def all_inner_texts(self):
                return self.texts

            @property
            def last(self):
                return self

            def inner_text(self):
                return "Merhaba"

            def press(self, key):
                self.press_count += 1
                self.asserted_key = key

        class FakePage:
            def __init__(self, case):
                self.case = case
                self.compose = Locator()
                self.outgoing = Locator()

            def goto(self, url):
                self.url = url

            def wait_for_selector(self, selector, *, state=None, timeout=None):
                self.case.assertEqual(selector, wa_panel.INPUT_SELECTOR)
                self.case.assertEqual(state, "visible")
                return object()

            def query_selector(self, selector):
                return object()

            def locator(self, selector):
                return self.outgoing if selector == "div.message-out" else self.compose

            def wait_for_function(self, expression, *, arg=None, timeout=None):
                self.case.assertIsNotNone(arg)
                self.case.assertEqual(arg["text"], "Merhaba")

        page = FakePage(self)
        with patch.object(wa_panel.time, "sleep"), patch.object(wa_panel, "wait_for_link_preview"):
            result, note = wa_panel.send_message(page, "905551234567", "Merhaba")

        self.assertEqual((result, note), ("ok", ""))
        self.assertEqual(page.compose.press_count, 1)

    def test_template_recipient_view_backfills_legacy_history(self):
        client = wa_panel.app.test_client()
        created = client.post("/api/templates", json={
            "name": "Deneme sablonu", "message": "Merhaba"
        })
        self.assertEqual(created.status_code, 200)
        template = created.get_json()["item"]

        # Eski surumde kaydedilmis, template_id alani bos olan gonderim.
        conn = sqlite3.connect(wa_panel.HISTORY_DB)
        try:
            conn.execute("""INSERT INTO send_history
                (phone,name,message,sent_at,job_id,job_name,result,message_key,message_id,template_id)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                ("905550000001", "Test Kisi", "Merhaba", "2026-10-16T12:00:00",
                 "job-old", "Eski gorev", "ok", wa_panel.message_fingerprint("Merhaba"),
                 template["message_id"], ""))
            conn.commit()
        finally:
            conn.close()

        response = client.get(f"/api/templates/{template['id']}/recipients")
        self.assertEqual(response.status_code, 200)
        items = response.get_json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["result"], "ok")

    def test_editing_template_keeps_dedup_identity_and_image(self):
        wa_panel.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        image_name = "a" * 32 + ".png"
        (wa_panel.IMAGES_DIR / image_name).write_bytes(b"test")
        client = wa_panel.app.test_client()
        created = client.post("/api/templates", json={
            "name": "Sablon", "message": "Ilk metin", "image": image_name
        })
        self.assertEqual(created.status_code, 200)
        item = created.get_json()["item"]
        message_id = item["message_id"]
        recipient = {"phone": "905550000002", "name": "Test", "template_id": item["id"]}
        job = {"id": "template-job", "name": "Sablon gorevi", "image": item["image"],
               "template_id": item["id"]}
        key = wa_panel.message_fingerprint("Ilk metin", item["image"])
        attempt = wa_panel.record_attempt(job, recipient, "Ilk metin", "belirsiz", "sending",
                                          key, message_id)
        wa_panel.finish_attempt(attempt, "ok")

        updated = client.put(f"/api/templates/{item['id']}", json={
            "name": "Sablon", "message": "Guncellenmis metin", "image": image_name
        })
        self.assertEqual(updated.status_code, 200)
        result = updated.get_json()["item"]
        self.assertEqual(result["message_id"], message_id)
        self.assertEqual(result["image"], image_name)
        self.assertTrue(wa_panel.previously_sent(recipient["phone"], result["message_id"],
                                                 wa_panel.message_fingerprint("Guncellenmis metin")))

    def test_csv_preview_selection_excludes_unchecked_people_from_job(self):
        client = wa_panel.app.test_client()
        response = client.post("/api/jobs/csv", data={
            "file": (BytesIO(b"phone,name\n905550000005,A\n905550000006,B\n"), "contacts.csv"),
            "message": "Merhaba", "send_phones": '["905550000006"]',
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 200, response.get_json())
        job = wa_panel.jobs[-1]
        self.assertEqual([r["phone"] for r in job["recipients"]], ["905550000006"])
        self.assertTrue(any(x["raw"] == "905550000005" and "atla" in x["reason"]
                            for x in job["skipped"]))

    def test_running_csv_job_is_marked_for_resume_with_checkpoint(self):
        job = {
            "id": "resume-job", "type": "liste", "name": "CSV listesi", "status": "calisiyor",
            "next_run": "2026-10-16T12:00:00", "daily_limit": 20, "sent_today": 1,
            "recipients": [{"phone": "905550000007", "name": "Test", "row": 8, "status": "bekliyor"}],
            "current_recipient": {"phone": "905550000007", "name": "Test", "row": 8},
        }
        wa_panel.JOBS_FILE.write_text(json.dumps([job]), encoding="utf-8")

        wa_panel.load_jobs()

        resumed = wa_panel.jobs[0]
        self.assertEqual(resumed["status"], "bekliyor")
        self.assertLessEqual(wa_panel.datetime.fromisoformat(resumed["next_run"]), wa_panel.datetime.now())
        self.assertIn("kaldigi yerden", resumed["resume_notice"])
        self.assertTrue(wa_panel.JOBS_FILE.with_name("jobs.json.bak").exists())

    def test_corrupt_jobs_file_recovers_from_atomic_backup(self):
        original = [{"id": "saved-job", "status": "bekliyor", "next_run": "2026-10-16T12:00:00"}]
        wa_panel.JOBS_FILE.write_text(json.dumps(original), encoding="utf-8")
        wa_panel.jobs = [{"id": "new-job", "status": "bekliyor", "next_run": "2026-10-16T13:00:00"}]
        wa_panel.save_jobs()
        wa_panel.JOBS_FILE.write_text("{corrupt", encoding="utf-8")

        wa_panel.load_jobs()

        self.assertEqual(wa_panel.jobs[0]["id"], "saved-job")
        self.assertEqual(json.loads(wa_panel.JOBS_FILE.read_text(encoding="utf-8"))[0]["id"], "saved-job")


if __name__ == "__main__":
    unittest.main()
