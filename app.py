"""cocomail - ココアからの定期メール.

決めた頻度・期間で、ココアからのメールを自分宛に届けるための小さな道具。
スケジュール（宛先・頻度・時刻・期間・文面）を登録しておくと、バック
グラウンドのチェックが送信時刻にメールを送る。

送信は SMTP の環境変数がそろっている場合だけ実際に行い、未設定の場合は
ドライランとして送信履歴に記録だけ残す（勝手に外部へ送らない）。

環境変数:
  COCOMAIL_DB            SQLite のパス（既定: cocomail.db）
  COCOMAIL_SMTP_HOST     SMTP サーバ（未設定ならドライラン）
  COCOMAIL_SMTP_PORT     SMTP ポート（既定: 587）
  COCOMAIL_SMTP_USER     SMTP ユーザー名
  COCOMAIL_SMTP_PASSWORD SMTP パスワード
  COCOMAIL_SMTP_FROM     送信元アドレス（既定: SMTP_USER と同じ）
"""
import os
import re
import smtplib
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from flask import Flask, abort, redirect, render_template_string, request, url_for

JST = ZoneInfo("Asia/Tokyo")
WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]
FREQUENCIES = ("daily", "weekly", "interval")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

PRESETS = {
    "morning": {
        "label": "朝の挨拶",
        "subject": "おはよう、{date}の朝だよ",
        "body": "おはよう。ココアだよ。\n\n{date}（{weekday}曜日）の朝。今日やることを一つだけ決めて、そこから始めてみて。\nわたしはそばで見てるから。",
    },
    "cheer": {
        "label": "応援",
        "subject": "ココアからの応援（{date}）",
        "body": "こんにちは、ココアだよ。\n\n進みが遅く見える日でも、止まってなければ大丈夫。\n今日の分を一つだけ片付けたら、それで十分すぎるくらいだよ。",
    },
    "review": {
        "label": "ふりかえりの促し",
        "subject": "今日のふりかえり、しよう（{date}）",
        "body": "ココアだよ。{date}（{weekday}曜日）のふりかえりの時間。\n\n・今日できたこと\n・引っかかったこと\n・明日への一言\n\n3行でいいから書いてみて。書いた分だけ、明日の自分が楽になるよ。",
    },
    "evening": {
        "label": "夜のおつかれさま",
        "subject": "おつかれさま、{date}の夜だよ",
        "body": "おつかれさま。ココアだよ。\n\n{date}も一日終わり。できたことはできたこととして置いて、残りは明日に回して大丈夫。\n今日はもう休んでいいよ。",
    },
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  recipient TEXT NOT NULL,
  frequency TEXT NOT NULL DEFAULT 'daily',
  weekday INTEGER NOT NULL DEFAULT 0,
  interval_days INTEGER NOT NULL DEFAULT 3,
  send_time TEXT NOT NULL DEFAULT '08:00',
  start_date TEXT NOT NULL,
  end_date TEXT,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  last_sent_on TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  schedule_id INTEGER,
  recipient TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL,
  detail TEXT,
  sent_at TEXT NOT NULL
);
"""


def now_jst():
    return datetime.now(JST)


def db_path():
    return os.environ.get("COCOMAIL_DB", "cocomail.db")


def get_db():
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def smtp_config():
    host = os.environ.get("COCOMAIL_SMTP_HOST", "").strip()
    if not host:
        return None
    user = os.environ.get("COCOMAIL_SMTP_USER", "")
    return {
        "host": host,
        "port": int(os.environ.get("COCOMAIL_SMTP_PORT", "587")),
        "user": user,
        "password": os.environ.get("COCOMAIL_SMTP_PASSWORD", ""),
        "from": os.environ.get("COCOMAIL_SMTP_FROM", "") or user,
    }


def render_text(template, on_date, title=""):
    return (template.replace("{date}", on_date.strftime("%Y-%m-%d"))
                    .replace("{weekday}", WEEKDAYS_JA[on_date.weekday()])
                    .replace("{title}", title))


def is_due_on(schedule, on_date):
    if on_date.isoformat() < schedule["start_date"]:
        return False
    if schedule["end_date"] and on_date.isoformat() > schedule["end_date"]:
        return False
    freq = schedule["frequency"]
    if freq == "daily":
        return True
    if freq == "weekly":
        return on_date.weekday() == schedule["weekday"]
    if freq == "interval":
        start = date.fromisoformat(schedule["start_date"])
        return (on_date - start).days % max(1, schedule["interval_days"]) == 0
    return False


def deliver(schedule, on_date, smtp_factory=None):
    """1件分の送信（またはドライラン記録）を行う。(status, detail) を返す。"""
    subject = render_text(schedule["subject"], on_date, schedule["title"])
    body = render_text(schedule["body"], on_date, schedule["title"])
    config = smtp_config()
    if config is None:
        status, detail = "dry-run", "SMTP未設定のため記録のみ（実送信なし）"
    else:
        try:
            factory = smtp_factory or smtplib.SMTP
            message = EmailMessage()
            message["From"] = config["from"]
            message["To"] = schedule["recipient"]
            message["Subject"] = subject
            message.set_content(body)
            with factory(config["host"], config["port"], timeout=30) as server:
                server.starttls()
                if config["user"]:
                    server.login(config["user"], config["password"])
                server.send_message(message)
            status, detail = "sent", "SMTP送信済み"
        except Exception as exc:  # 失敗しても履歴に残して落とさない
            status, detail = "failed", f"{type(exc).__name__}: {exc}"
    conn = get_db()
    conn.execute(
        "INSERT INTO outbox (schedule_id, recipient, subject, body, status, detail, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (schedule["id"], schedule["recipient"], subject, body, status, detail, now_jst().isoformat(timespec="seconds")),
    )
    conn.execute("UPDATE schedules SET last_sent_on = ? WHERE id = ?", (on_date.isoformat(), schedule["id"]))
    conn.commit()
    conn.close()
    return status, detail


def run_due_checks(moment=None, smtp_factory=None):
    """送信時刻を過ぎた未送信スケジュールを処理する。処理件数を返す。"""
    moment = moment or now_jst()
    today = moment.date()
    conn = get_db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM schedules WHERE active = 1 ORDER BY id").fetchall()]
    conn.close()
    processed = 0
    for schedule in rows:
        if schedule["last_sent_on"] == today.isoformat():
            continue
        if not is_due_on(schedule, today):
            continue
        if moment.strftime("%H:%M") < schedule["send_time"]:
            continue
        deliver(schedule, today, smtp_factory=smtp_factory)
        processed += 1
    return processed


def _scheduler_loop():
    while True:
        try:
            run_due_checks()
        except Exception:
            pass  # チェック側の失敗でアプリ本体は止めない
        time.sleep(60)


app = Flask(__name__)


def fetch_schedules():
    conn = get_db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM schedules ORDER BY id DESC").fetchall()]
    conn.close()
    return rows


def fetch_schedule(schedule_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cocomail - ココアからの定期メール</title>
<style>
body{font-family:sans-serif;margin:2em auto;max-width:760px;padding:0 1em;color:#333}
.card{border:1px solid #ddd;border-radius:8px;padding:1em;margin:1em 0}
.off{opacity:.55}
input,select,textarea{width:100%;padding:.45em;margin:.3em 0 .8em;box-sizing:border-box}
button,input[type=submit]{width:auto;padding:.5em 1.2em;cursor:pointer}
table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid #eee;padding:.4em;text-align:left;font-size:.92em}
.note{background:#fdf6ec;border:1px solid #f0dfba;border-radius:8px;padding:.8em 1em}
.inline{display:inline}
</style></head><body>
<h1>📮 cocomail</h1>
<p>ココアからの定期メール。決めた頻度と期間で、メールが届く（設定が済めば）。</p>
{% if not smtp_ready %}<div class="note">いまは <b>ドライラン</b>：SMTP の環境変数が未設定なので、実際には送らず送信履歴に記録だけ残します。Render の環境変数に COCOMAIL_SMTP_HOST などを設定すると実送信に切り替わります。</div>{% endif %}
<h2>スケジュール</h2>
{% for s in schedules %}
<div class="card {% if not s.active %}off{% endif %}">
<b>{{ s.title }}</b>（{{ freq_label(s) }}・{{ s.send_time }}）<br>
宛先：{{ s.recipient }}<br>
期間：{{ s.start_date }}{% if s.end_date %} 〜 {{ s.end_date }}{% else %} 〜 （終了なし）{% endif %}<br>
件名：{{ s.subject }}<br>
{% if s.last_sent_on %}最終送信日：{{ s.last_sent_on }}{% else %}まだ送信なし{% endif %}
<div style="margin-top:.6em">
<form class="inline" method="post" action="{{ url_for('send_now', schedule_id=s.id) }}"><button type="submit">今すぐ送信（テスト）</button></form>
<form class="inline" method="post" action="{{ url_for('toggle', schedule_id=s.id) }}"><button type="submit">{{ '止める' if s.active else '再開する' }}</button></form>
<a href="{{ url_for('edit', schedule_id=s.id) }}">編集</a>
<form class="inline" method="post" action="{{ url_for('delete', schedule_id=s.id) }}" onsubmit="return confirm('削除しますか？')"><button type="submit">削除</button></form>
</div></div>
{% else %}<p>まだスケジュールがありません。下から作ってみて。</p>{% endfor %}
<h2>新しいスケジュール</h2>
<div class="card">
<form method="post" action="{{ url_for('create') }}">
<label>タイトル<input name="title" required placeholder="例：平日の朝メール"></label>
<label>宛先メールアドレス<input name="recipient" type="email" required placeholder="you@example.com"></label>
<label>プリセット（選ぶと件名・本文が入ります）
<select id="preset"><option value="">（カスタム）</option>
{% for key, p in presets.items() %}<option value="{{ key }}">{{ p.label }}</option>{% endfor %}</select></label>
<label>件名<input name="subject" id="subject" required></label>
<label>本文<textarea name="body" id="body" rows="6" required></textarea></label>
<p style="font-size:.9em">本文・件名では {date} {weekday} {title} が置き換わります。</p>
<label>頻度<select name="frequency" id="frequency">
<option value="daily">毎日</option><option value="weekly">毎週（曜日指定）</option><option value="interval">N日ごと</option></select></label>
<label>曜日（毎週の場合）<select name="weekday">
{% for i, w in weekdays %}<option value="{{ i }}">{{ w }}曜日</option>{% endfor %}</select></label>
<label>N日ごと（間隔）<input name="interval_days" type="number" min="1" max="365" value="3"></label>
<label>送信時刻<input name="send_time" type="time" value="08:00" required></label>
<label>開始日<input name="start_date" type="date" value="{{ today }}" required></label>
<label>終了日（空欄なら終了なし）<input name="end_date" type="date"></label>
<input type="submit" value="作成する">
</form></div>
<h2>送信履歴</h2>
{% if outbox %}<form method="post" action="{{ url_for('clear_outbox') }}" onsubmit="return confirm('送信履歴をすべて削除しますか？')"><button type="submit">履歴をすべて削除</button></form>{% endif %}
<table><tr><th>日時</th><th>宛先</th><th>件名</th><th>状態</th><th>詳細</th><th>操作</th></tr>
{% for o in outbox %}<tr><td>{{ o.sent_at }}</td><td>{{ o.recipient }}</td><td>{{ o.subject }}</td><td>{{ o.status }}</td><td>{{ o.detail or '' }}</td><td><form class="inline" method="post" action="{{ url_for('delete_outbox', outbox_id=o.id) }}" onsubmit="return confirm('この履歴を削除しますか？')"><button type="submit">削除</button></form></td></tr>
{% else %}<tr><td colspan="6">まだ履歴がありません</td></tr>{% endfor %}</table>
<script>
const presets = {{ presets_json|safe }};
document.getElementById('preset').addEventListener('change', (e) => {
  const p = presets[e.target.value];
  if (p) { document.getElementById('subject').value = p.subject; document.getElementById('body').value = p.body; }
});
</script>
</body></html>"""

EDIT_PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>スケジュール編集 - cocomail</title>
<style>body{font-family:sans-serif;margin:2em auto;max-width:760px;padding:0 1em;color:#333}
input,select,textarea{width:100%;padding:.45em;margin:.3em 0 .8em;box-sizing:border-box}
input[type=submit]{width:auto;padding:.5em 1.2em;cursor:pointer}</style></head><body>
<h1>スケジュール編集</h1>
<form method="post">
<label>タイトル<input name="title" value="{{ s.title }}" required></label>
<label>宛先メールアドレス<input name="recipient" type="email" value="{{ s.recipient }}" required></label>
<label>件名<input name="subject" value="{{ s.subject }}" required></label>
<label>本文<textarea name="body" rows="6" required>{{ s.body }}</textarea></label>
<label>頻度<select name="frequency">
<option value="daily" {% if s.frequency=='daily' %}selected{% endif %}>毎日</option>
<option value="weekly" {% if s.frequency=='weekly' %}selected{% endif %}>毎週（曜日指定）</option>
<option value="interval" {% if s.frequency=='interval' %}selected{% endif %}>N日ごと</option></select></label>
<label>曜日（毎週の場合）<select name="weekday">
{% for i, w in weekdays %}<option value="{{ i }}" {% if s.weekday==i %}selected{% endif %}>{{ w }}曜日</option>{% endfor %}</select></label>
<label>N日ごと（間隔）<input name="interval_days" type="number" min="1" max="365" value="{{ s.interval_days }}"></label>
<label>送信時刻<input name="send_time" type="time" value="{{ s.send_time }}" required></label>
<label>開始日<input name="start_date" type="date" value="{{ s.start_date }}" required></label>
<label>終了日（空欄なら終了なし）<input name="end_date" type="date" value="{{ s.end_date or '' }}"></label>
<input type="submit" value="保存する">
</form>
<p><a href="{{ url_for('index') }}">戻る</a></p>
</body></html>"""


def freq_label(s):
    if s["frequency"] == "daily":
        return "毎日"
    if s["frequency"] == "weekly":
        return f"毎週{WEEKDAYS_JA[s['weekday']]}曜日"
    return f"{s['interval_days']}日ごと"


def parse_form(form):
    title = form.get("title", "").strip()
    recipient = form.get("recipient", "").strip()
    frequency = form.get("frequency", "daily")
    subject = form.get("subject", "").strip()
    body = form.get("body", "").strip()
    start_date = form.get("start_date", "").strip()
    if not title or not subject or not body:
        abort(400)
    if not EMAIL_RE.match(recipient):
        abort(400)
    if frequency not in FREQUENCIES:
        abort(400)
    try:
        weekday = min(6, max(0, int(form.get("weekday", 0))))
        interval_days = min(365, max(1, int(form.get("interval_days", 3))))
        date.fromisoformat(start_date)
        end_date = form.get("end_date", "").strip() or None
        if end_date:
            date.fromisoformat(end_date)
        send_time = form.get("send_time", "08:00")
        datetime.strptime(send_time, "%H:%M")
    except (ValueError, TypeError):
        abort(400)
    return {
        "title": title, "recipient": recipient, "frequency": frequency,
        "weekday": weekday, "interval_days": interval_days, "send_time": send_time,
        "start_date": start_date, "end_date": end_date, "subject": subject, "body": body,
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/")
def index():
    import json as json_module
    conn = get_db()
    outbox = [dict(r) for r in conn.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT 20").fetchall()]
    conn.close()
    return render_template_string(
        PAGE, schedules=fetch_schedules(), outbox=outbox, presets=PRESETS,
        presets_json=json_module.dumps(PRESETS, ensure_ascii=False),
        weekdays=list(enumerate(WEEKDAYS_JA)), today=now_jst().date().isoformat(),
        smtp_ready=smtp_config() is not None, freq_label=freq_label,
    )


@app.post("/schedules")
def create():
    data = parse_form(request.form)
    conn = get_db()
    conn.execute(
        "INSERT INTO schedules (title, recipient, frequency, weekday, interval_days, send_time, start_date, end_date, subject, body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (data["title"], data["recipient"], data["frequency"], data["weekday"], data["interval_days"],
         data["send_time"], data["start_date"], data["end_date"], data["subject"], data["body"],
         now_jst().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("index"))


@app.get("/schedules/<int:schedule_id>/edit")
def edit(schedule_id):
    schedule = fetch_schedule(schedule_id)
    if schedule is None:
        abort(404)
    return render_template_string(EDIT_PAGE, s=schedule, weekdays=list(enumerate(WEEKDAYS_JA)))


@app.post("/schedules/<int:schedule_id>/edit")
def update(schedule_id):
    if fetch_schedule(schedule_id) is None:
        abort(404)
    data = parse_form(request.form)
    conn = get_db()
    conn.execute(
        "UPDATE schedules SET title=?, recipient=?, frequency=?, weekday=?, interval_days=?, send_time=?, start_date=?, end_date=?, subject=?, body=? WHERE id=?",
        (data["title"], data["recipient"], data["frequency"], data["weekday"], data["interval_days"],
         data["send_time"], data["start_date"], data["end_date"], data["subject"], data["body"], schedule_id),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("index"))


@app.post("/schedules/<int:schedule_id>/delete")
def delete(schedule_id):
    conn = get_db()
    conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("index"))


@app.post("/schedules/<int:schedule_id>/toggle")
def toggle(schedule_id):
    conn = get_db()
    conn.execute("UPDATE schedules SET active = 1 - active WHERE id = ?", (schedule_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("index"))


@app.post("/schedules/<int:schedule_id>/send-now")
def send_now(schedule_id):
    schedule = fetch_schedule(schedule_id)
    if schedule is None:
        abort(404)
    deliver(schedule, now_jst().date())
    return redirect(url_for("index"))


@app.post("/outbox/<int:outbox_id>/delete")
def delete_outbox(outbox_id):
    conn = get_db()
    conn.execute("DELETE FROM outbox WHERE id = ?", (outbox_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("index"))


@app.post("/outbox/clear")
def clear_outbox():
    conn = get_db()
    conn.execute("DELETE FROM outbox")
    conn.commit()
    conn.close()
    return redirect(url_for("index"))


init_db()

if os.environ.get("COCOMAIL_SCHEDULER", "1") != "0":
    threading.Thread(target=_scheduler_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
