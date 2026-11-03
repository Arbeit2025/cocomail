import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["COCOMAIL_SCHEDULER"] = "0"

import app as cocomail  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.messages = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        self.user = user

    def send_message(self, message):
        self.messages.append(message)


def failing_smtp(host, port, timeout=None):
    raise OSError("connection refused")


class CocomailTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_file = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db_file)
        os.environ["COCOMAIL_DB"] = self.db_file
        for key in list(os.environ):
            if key.startswith("COCOMAIL_SMTP"):
                del os.environ[key]
        cocomail.init_db()
        self.client = cocomail.app.test_client()
        FakeSMTP.instances = []

    def tearDown(self):
        if os.path.exists(self.db_file):
            os.unlink(self.db_file)

    def make_schedule(self, **overrides):
        data = {
            "title": "朝メール", "recipient": "me@example.com", "frequency": "daily",
            "weekday": 0, "interval_days": 3, "send_time": "08:00",
            "start_date": "2026-11-01", "end_date": None,
            "subject": "件名 {date}", "body": "本文 {weekday}",
        }
        data.update(overrides)
        conn = cocomail.get_db()
        cur = conn.execute(
            "INSERT INTO schedules (title, recipient, frequency, weekday, interval_days, send_time, start_date, end_date, subject, body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (data["title"], data["recipient"], data["frequency"], data["weekday"], data["interval_days"],
             data["send_time"], data["start_date"], data["end_date"], data["subject"], data["body"],
             "2026-11-01T00:00:00+09:00"),
        )
        conn.commit()
        schedule_id = cur.lastrowid
        conn.close()
        return cocomail.fetch_schedule(schedule_id)

    def test_healthz(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "ok"})

    def test_create_schedule_via_form(self):
        resp = self.client.post("/schedules", data={
            "title": "平日の朝", "recipient": "me@example.com", "frequency": "weekly",
            "weekday": "0", "interval_days": "3", "send_time": "07:30",
            "start_date": "2026-11-03", "end_date": "2026-12-31",
            "subject": "おはよう", "body": "本文だよ",
        })
        self.assertEqual(resp.status_code, 302)
        schedules = cocomail.fetch_schedules()
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0]["title"], "平日の朝")
        self.assertEqual(schedules[0]["weekday"], 0)
        self.assertEqual(schedules[0]["end_date"], "2026-12-31")
        top = self.client.get("/")
        self.assertIn("平日の朝", top.get_data(as_text=True))

    def test_create_rejects_invalid_email(self):
        resp = self.client.post("/schedules", data={
            "title": "x", "recipient": "not-an-email", "frequency": "daily",
            "weekday": "0", "interval_days": "3", "send_time": "08:00",
            "start_date": "2026-11-03", "end_date": "", "subject": "s", "body": "b",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(cocomail.fetch_schedules(), [])

    def test_due_daily_within_period(self):
        s = self.make_schedule(start_date="2026-11-01", end_date="2026-11-10")
        self.assertTrue(cocomail.is_due_on(s, date(2026, 11, 3)))
        self.assertFalse(cocomail.is_due_on(s, date(2026, 10, 31)))
        self.assertFalse(cocomail.is_due_on(s, date(2026, 11, 11)))

    def test_due_weekly_only_on_weekday(self):
        # 2026-11-02 is a Monday (weekday 0)
        s = self.make_schedule(frequency="weekly", weekday=0)
        self.assertTrue(cocomail.is_due_on(s, date(2026, 11, 2)))
        self.assertFalse(cocomail.is_due_on(s, date(2026, 11, 3)))

    def test_due_interval(self):
        s = self.make_schedule(frequency="interval", interval_days=3, start_date="2026-11-01")
        self.assertTrue(cocomail.is_due_on(s, date(2026, 11, 1)))
        self.assertFalse(cocomail.is_due_on(s, date(2026, 11, 2)))
        self.assertTrue(cocomail.is_due_on(s, date(2026, 11, 4)))

    def test_render_text_placeholders(self):
        out = cocomail.render_text("{date}は{weekday}曜日・{title}", date(2026, 11, 2), "朝メール")
        self.assertEqual(out, "2026-11-02は月曜日・朝メール")

    def test_deliver_dry_run_records_outbox(self):
        s = self.make_schedule()
        status, _ = cocomail.deliver(s, date(2026, 11, 3))
        self.assertEqual(status, "dry-run")
        conn = cocomail.get_db()
        rows = conn.execute("SELECT * FROM outbox").fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject"], "件名 2026-11-03")
        self.assertEqual(cocomail.fetch_schedule(s["id"])["last_sent_on"], "2026-11-03")

    def test_deliver_with_smtp_sends(self):
        os.environ["COCOMAIL_SMTP_HOST"] = "smtp.example.com"
        os.environ["COCOMAIL_SMTP_USER"] = "cocoa@example.com"
        os.environ["COCOMAIL_SMTP_PASSWORD"] = "secret"
        s = self.make_schedule()
        status, _ = cocomail.deliver(s, date(2026, 11, 3), smtp_factory=FakeSMTP)
        self.assertEqual(status, "sent")
        self.assertEqual(len(FakeSMTP.instances), 1)
        self.assertEqual(len(FakeSMTP.instances[0].messages), 1)
        self.assertEqual(FakeSMTP.instances[0].messages[0]["To"], "me@example.com")

    def test_deliver_smtp_failure_is_recorded_not_raised(self):
        os.environ["COCOMAIL_SMTP_HOST"] = "smtp.example.com"
        s = self.make_schedule()
        status, detail = cocomail.deliver(s, date(2026, 11, 3), smtp_factory=failing_smtp)
        self.assertEqual(status, "failed")
        self.assertIn("OSError", detail)

    def test_run_due_checks_processes_due_schedule_once(self):
        s = self.make_schedule(send_time="08:00")
        moment = datetime(2026, 11, 3, 9, 0, tzinfo=JST)
        self.assertEqual(cocomail.run_due_checks(moment), 1)
        self.assertEqual(cocomail.run_due_checks(moment), 0)  # 二重送信しない

    def test_run_due_checks_respects_send_time(self):
        self.make_schedule(send_time="08:00")
        moment = datetime(2026, 11, 3, 7, 0, tzinfo=JST)
        self.assertEqual(cocomail.run_due_checks(moment), 0)

    def test_run_due_checks_skips_inactive(self):
        s = self.make_schedule()
        conn = cocomail.get_db()
        conn.execute("UPDATE schedules SET active = 0 WHERE id = ?", (s["id"],))
        conn.commit()
        conn.close()
        moment = datetime(2026, 11, 3, 9, 0, tzinfo=JST)
        self.assertEqual(cocomail.run_due_checks(moment), 0)

    def test_toggle_and_delete(self):
        s = self.make_schedule()
        self.client.post(f"/schedules/{s['id']}/toggle")
        self.assertEqual(cocomail.fetch_schedule(s["id"])["active"], 0)
        self.client.post(f"/schedules/{s['id']}/toggle")
        self.assertEqual(cocomail.fetch_schedule(s["id"])["active"], 1)
        self.client.post(f"/schedules/{s['id']}/delete")
        self.assertIsNone(cocomail.fetch_schedule(s["id"]))

    def test_edit_updates_schedule(self):
        s = self.make_schedule()
        resp = self.client.post(f"/schedules/{s['id']}/edit", data={
            "title": "夜メール", "recipient": "me@example.com", "frequency": "daily",
            "weekday": "0", "interval_days": "3", "send_time": "21:00",
            "start_date": "2026-11-01", "end_date": "", "subject": "おつかれさま", "body": "本文",
        })
        self.assertEqual(resp.status_code, 302)
        updated = cocomail.fetch_schedule(s["id"])
        self.assertEqual(updated["title"], "夜メール")
        self.assertEqual(updated["send_time"], "21:00")

    def test_send_now_records_outbox(self):
        s = self.make_schedule()
        resp = self.client.post(f"/schedules/{s['id']}/send-now")
        self.assertEqual(resp.status_code, 302)
        conn = cocomail.get_db()
        count = conn.execute("SELECT COUNT(*) AS c FROM outbox").fetchone()["c"]
        conn.close()
        self.assertEqual(count, 1)

    def _outbox_rows(self):
        conn = cocomail.get_db()
        rows = [dict(r) for r in conn.execute("SELECT * FROM outbox ORDER BY id").fetchall()]
        conn.close()
        return rows

    def test_delete_outbox_entry(self):
        s = self.make_schedule()
        self.client.post(f"/schedules/{s['id']}/send-now")
        self.client.post(f"/schedules/{s['id']}/send-now")
        rows = self._outbox_rows()
        self.assertEqual(len(rows), 2)
        resp = self.client.post(f"/outbox/{rows[0]['id']}/delete")
        self.assertEqual(resp.status_code, 302)
        rows = self._outbox_rows()
        self.assertEqual(len(rows), 1)
        # スケジュール自体は残る
        self.assertIsNotNone(cocomail.fetch_schedule(s["id"]))

    def test_clear_outbox(self):
        s = self.make_schedule()
        self.client.post(f"/schedules/{s['id']}/send-now")
        self.client.post(f"/schedules/{s['id']}/send-now")
        self.assertEqual(len(self._outbox_rows()), 2)
        resp = self.client.post("/outbox/clear")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._outbox_rows(), [])

    def test_delete_missing_outbox_entry_is_ok(self):
        resp = self.client.post("/outbox/999/delete")
        self.assertEqual(resp.status_code, 302)

    def test_presets_have_subject_and_body(self):
        self.assertGreaterEqual(len(cocomail.PRESETS), 3)
        for preset in cocomail.PRESETS.values():
            self.assertTrue(preset["subject"])
            self.assertTrue(preset["body"])


if __name__ == "__main__":
    unittest.main()
