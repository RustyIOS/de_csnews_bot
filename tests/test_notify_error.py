"""Private failure DMs. Telegram is mocked; these tests must not send."""
import datetime
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notify_error
import scripts.gha_run as gha_run
from common import CHANNEL_ID

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MSK = ZoneInfo("Europe/Moscow")
TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
CHAT = "424242424"
WHEN = datetime.datetime(2026, 11, 7, 10, 14, tzinfo=MSK)
GEMINI = "AIzaSyA1234567890abcdefghijklmnop"
ANNOUNCE_LOG = (
    "UNIQUE_MIDDLE_LOG_LINE\n" * 30
    + "[2026-11-07 10:14:00] text:\n"
    + "<b>Матчи дня, 7 ноября</b>\n"
    + "18:00 NAVI vs Spirit, bo3\n"
    + "[2026-11-07 10:14:08] ABORT: check_post.py FAIL (see above). Nothing sent.\n"
    + "[2026-11-07 10:14:08] FAILED\n"
)


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _lines(text):
    return [line.strip() for line in text.splitlines()]


class AlertCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dedup = os.path.join(self.tmp.name, "alert_dedup.json")
        self.report = os.path.join(self.tmp.name, "publish_failure.json")
        self.env = patch.dict(os.environ, {
            "ALERT_DEDUP_PATH": self.dedup,
            "PUBLISH_FAILURE_REPORT": self.report,
            "TELEGRAM_BOT_TOKEN": TOKEN,
            "TELEGRAM_ALERT_CHAT_ID": CHAT,
            "NOTIFY_FORBID_NETWORK": "1",
            "GEMINI_API_KEY": "",
            "OPENROUTER_API_KEY": "",
            "GITHUB_TOKEN": "",
            "GH_TOKEN": "",
            "GITHUB_ACTIONS": "",
            "GITHUB_SERVER_URL": "",
            "GITHUB_REPOSITORY": "",
            "GITHUB_RUN_ID": "",
            "GHA_DRY_RUN": "",
            "GHA_JOB": "",
            "GHA_MODE": "",
            "GITHUB_EVENT_NAME": "",
            "GITHUB_EVENT_SCHEDULE": "",
            "POSTING_ENABLED": "",
            "TELEGRAM_CHAT_ID": CHANNEL_ID,
            "CHAT_ID": CHANNEL_ID,
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self._http = patch("requests.post", side_effect=AssertionError("network"))
        self.http = self._http.start()
        self.addCleanup(self._http.stop)
        self._cwd = os.getcwd()
        self.addCleanup(os.chdir, self._cwd)
        os.chdir(self.tmp.name)

    def _allow_mock_http(self, body=None, status=200):
        os.environ["NOTIFY_FORBID_NETWORK"] = ""
        self.http.side_effect = None
        self.http.return_value = FakeResponse(status, {"ok": True, "result": {"message_id": 1}} if body is None else body)

    def _payload(self):
        self.assertTrue(self.http.called)
        kwargs = self.http.call_args.kwargs
        args = self.http.call_args.args
        url = args[0] if args else kwargs.get("url")
        body = kwargs.get("json") or {}
        return url, body


class NotifyErrorTest(AlertCase):
    def test_success_private_chat_only(self):
        self._allow_mock_http()
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = notify_error.notify(
                platform="telegram", stage="announce", title="Матчи дня, 7 ноября",
                error="Telegram error: chat not found", status="not_posted",
                logs_url="https://github.com/RustyIOS/de_csnews_bot/actions/runs/999",
                now=WHEN,
            )
        self.assertTrue(result["delivered"])
        self.assertEqual(result["reason"], "delivered")
        self.assertIn("alert: delivered", _lines(buf.getvalue()))
        url, body = self._payload()
        self.assertTrue(url.endswith("/sendMessage"), url)
        self.assertIn(f"/bot{TOKEN}/", url)
        self.assertNotIn("sendPhoto", url)
        self.assertEqual(body["chat_id"], 424242424)
        self.assertIsInstance(body["chat_id"], int)
        text = body["text"]
        self.assertNotIn(CHANNEL_ID, text)
        self.assertNotIn(CHANNEL_ID, url)
        self.assertNotIn(CHANNEL_ID, json.dumps(body))
        self.assertIn("Площадка: telegram", text)
        self.assertIn("Этап: announce", text)
        self.assertIn("Время: 2026-11-07 10:14 MSK", text)
        self.assertIn("Статус: не отправлено", text)
        self.assertIn("Материал: Матчи дня, 7 ноября", text)
        self.assertIn("Причина: Telegram error: chat not found", text)
        self.assertIn("https://github.com/RustyIOS/de_csnews_bot/actions/runs/999", text)
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(self.http.call_args.kwargs["timeout"], 15)

    def test_cli_for_dzen_and_clips(self):
        self._allow_mock_http()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = notify_error.main([
                "--platform", "dzen", "--stage", "publish", "--title", "Ролик",
                "--error", "HTTP 500", "--status", "not_posted",
                "--logs-url", "https://example.invalid/job/1",
            ])
        self.assertEqual(rc, 0)
        _, body = self._payload()
        self.assertEqual(body["chat_id"], 424242424)
        self.assertIn("Площадка: dzen", body["text"])
        self.assertIn("Этап: publish", body["text"])
        self.assertIn("Материал: Ролик", body["text"])
        self.assertIn("Причина: HTTP 500", body["text"])
        self.assertIn("https://example.invalid/job/1", body["text"])
        self.assertNotIn(CHANNEL_ID, body["text"])

    def test_plus_and_quoted_chat_id(self):
        self._allow_mock_http()
        os.environ["TELEGRAM_ALERT_CHAT_ID"] = "+424242424"
        with redirect_stdout(io.StringIO()):
            notify_error.notify(platform="clips", stage="upload", error="ffmpeg exit 1", status="unknown", now=WHEN)
        _, body = self._payload()
        self.assertEqual(body["chat_id"], 424242424)
        os.environ["TELEGRAM_ALERT_CHAT_ID"] = '"424242424"'
        with redirect_stdout(io.StringIO()):
            notify_error.notify(
                platform="clips", stage="upload", error="ffmpeg exit 2", status="unknown",
                now=WHEN + datetime.timedelta(minutes=1),
            )
        self.assertEqual(self.http.call_args.kwargs["json"]["chat_id"], 424242424)

    def test_telegram_api_failure_is_not_claimed_delivered(self):
        self._allow_mock_http(body={"ok": False, "description": "Unauthorized", "error_code": 401})
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = notify_error.notify(
                platform="telegram", stage="news", error="send failed", status="not_posted", now=WHEN,
            )
        out = buf.getvalue()
        self.assertFalse(result["delivered"])
        self.assertEqual(result["reason"], "telegram_error")
        self.assertEqual(notify_error.exit_code(result["reason"]), 1)
        self.assertIn("alert: not delivered (Unauthorized)", _lines(out))
        self.assertNotIn("alert: delivered", _lines(out))
        self.assertNotIn(TOKEN, out)

    def test_transport_error_redacts_token_and_can_retry(self):
        import requests
        os.environ["NOTIFY_FORBID_NETWORK"] = ""
        secret_url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

        def boom(*_a, **_k):
            raise requests.ConnectionError(f"failed {secret_url} Authorization: Bearer {TOKEN}")

        self.http.side_effect = boom
        buf = io.StringIO()
        with redirect_stdout(buf):
            first = notify_error.notify(
                platform="telegram", stage="results", error="post.py failed", status="not_posted", now=WHEN,
            )
        out = buf.getvalue()
        self.assertFalse(first["delivered"])
        self.assertNotIn(TOKEN, out)
        self.assertNotIn("bot" + TOKEN, out)
        self.assertNotIn("Bearer " + TOKEN, out)
        self.assertIn("[redacted]", out)
        self.assertNotIn("alert: delivered", _lines(out))
        self.http.side_effect = None
        self.http.return_value = FakeResponse(200, {"ok": True, "result": {"message_id": 2}})
        with redirect_stdout(io.StringIO()):
            second = notify_error.notify(
                platform="telegram", stage="results", error="post.py failed", status="not_posted", now=WHEN,
            )
        self.assertTrue(second["delivered"])
        self.assertEqual(self.http.call_count, 2)

    def test_redaction_in_message_title_and_dedup_file(self):
        self._allow_mock_http()
        os.environ["GEMINI_API_KEY"] = GEMINI
        error = (
            f"see https://api.telegram.org/bot{TOKEN}/getUpdates?token={TOKEN} "
            f"Authorization: Bearer {TOKEN} GEMINI_API_KEY={GEMINI} "
            "sk-or-v1-abcdefghijklmnopqrstuvwxyz"
        )
        with redirect_stdout(io.StringIO()):
            result = notify_error.notify(
                platform="telegram", stage="news", title=f"leak {TOKEN}",
                error=error, status="unknown", now=WHEN,
            )
        self.assertTrue(result["delivered"])
        text = self.http.call_args.kwargs["json"]["text"]
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(GEMINI, text)
        self.assertNotIn("sk-or-v1-", text)
        self.assertNotIn("Bearer " + TOKEN, text)
        self.assertIn("[redacted]", text)
        self.assertIn("bot[redacted]", text)
        with open(self.dedup, encoding="utf-8") as handle:
            stored = handle.read()
        self.assertNotIn(TOKEN, stored)
        self.assertNotIn(GEMINI, stored)
        self.assertNotIn("sk-or-v1-", stored)
        self.assertNotIn("getUpdates", stored)

    def test_logs_url_with_bot_token_is_dropped(self):
        self._allow_mock_http()
        with redirect_stdout(io.StringIO()):
            notify_error.notify(
                platform="telegram", stage="live", error="gate broke", status="unknown",
                logs_url=f"https://api.telegram.org/bot{TOKEN}/sendMessage", now=WHEN,
            )
        text = self.http.call_args.kwargs["json"]["text"]
        self.assertNotIn("api.telegram.org", text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("Логи:", text)

    def test_no_target_and_no_public_fallback(self):
        os.environ.pop("TELEGRAM_ALERT_CHAT_ID", None)
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = notify_error.notify(
                platform="telegram", stage="announce", error="Telegram error: down", status="not_posted",
            )
        self.assertFalse(result["delivered"])
        self.assertEqual(result["reason"], "no_target")
        self.assertEqual(notify_error.exit_code("no_target"), 2)
        self.http.assert_not_called()
        self.assertIn("no private chat configured", buf.getvalue())
        self.assertNotIn("alert: delivered", _lines(buf.getvalue()))
        self.assertNotIn(CHANNEL_ID, buf.getvalue())

    def test_refuses_channel_ids(self):
        for raw in (CHANNEL_ID, "+1003635521376", "1003635521376", "3635521376", "@de_csnews",
                    "@DE_CSNEWS", "de_csnews", "0", "-1", "abc", "+0", ""):
            os.environ["TELEGRAM_ALERT_CHAT_ID"] = raw
            with self.subTest(raw=raw):
                self.http.reset_mock()
                buf = io.StringIO()
                with redirect_stdout(buf):
                    result = notify_error.notify(
                        platform="telegram", stage="announce", error="nope", status="not_posted", now=WHEN,
                    )
                self.assertFalse(result["delivered"])
                self.assertIn(result["reason"], ("refused_channel", "invalid_target", "no_target"))
                self.http.assert_not_called()
                self.assertNotIn("alert: delivered", _lines(buf.getvalue()))

    def test_missing_token_does_not_send(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = ""
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = notify_error.notify(
                platform="dzen", stage="publish", error="HTTP 500", status="not_posted", now=WHEN,
            )
        self.assertEqual(result["reason"], "no_token")
        self.http.assert_not_called()
        self.assertIn("TELEGRAM_BOT_TOKEN is not set", buf.getvalue())
        self.assertNotIn("alert: delivered", _lines(buf.getvalue()))

    def test_dry_run_does_not_send(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = notify_error.main([
                "--platform", "dzen", "--stage", "publish", "--error", "HTTP 500",
                "--status", "not_posted", "--dry-run",
            ])
        self.assertEqual(rc, 0)
        self.http.assert_not_called()
        self.assertIn("alert: dry-run, not sending", _lines(buf.getvalue()))
        self.assertNotIn("alert: delivered", _lines(buf.getvalue()))

    def test_duplicate_is_suppressed_until_window_passes(self):
        self._allow_mock_http()
        kwargs = dict(platform="telegram", stage="announce", title="Матчи дня", error="same boom", status="not_posted")
        with redirect_stdout(io.StringIO()):
            first = notify_error.notify(now=WHEN, **kwargs)
            again = notify_error.notify(now=WHEN + datetime.timedelta(minutes=149), **kwargs)
            other = notify_error.notify(now=WHEN + datetime.timedelta(minutes=1), platform="dzen", stage="announce",
                                        title="Матчи дня", error="same boom", status="not_posted")
            titled = notify_error.notify(now=WHEN + datetime.timedelta(minutes=2), platform="telegram",
                                         stage="announce", title="Другой заголовок", error="same boom",
                                         status="not_posted")
            later = notify_error.notify(now=WHEN + datetime.timedelta(minutes=150), **kwargs)
        self.assertEqual(first["reason"], "delivered")
        self.assertEqual(again["reason"], "duplicate")
        self.assertFalse(again["delivered"])
        self.assertEqual(other["reason"], "delivered")
        self.assertEqual(titled["reason"], "delivered")
        self.assertEqual(later["reason"], "delivered")
        self.assertEqual(self.http.call_count, 4)

    def test_missing_report_does_not_claim_delivery(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = notify_error.main(["--report", os.path.join(self.tmp.name, "missing.json")])
        self.assertEqual(rc, 0)
        self.http.assert_not_called()
        self.assertIn("alert: no failure report; not sending", _lines(buf.getvalue()))
        self.assertNotIn("alert: delivered", _lines(buf.getvalue()))

    def test_summary_keeps_title_and_drops_the_log(self):
        summary = notify_error.summarize_failure(ANNOUNCE_LOG, 1)
        self.assertEqual(summary["title"], "Матчи дня, 7 ноября")
        self.assertEqual(summary["status"], "not_posted")
        self.assertIn("Nothing sent", summary["error"])
        self.assertNotIn("UNIQUE_MIDDLE_LOG_LINE", summary["error"])
        self.assertNotIn("NAVI vs Spirit", summary["error"])
        self.assertLess(len(summary["error"]), 500)
        partial = '{"ok": true}\nTelegram error: second post failed\n'
        again = notify_error.summarize_failure(partial, 1)
        self.assertEqual(again["status"], "unknown")
        self.assertIn("Telegram error", again["error"])
        self.assertEqual(notify_error.summarize_failure("", 1)["status"], "unknown")

    def test_module_has_no_channel_fallback(self):
        with open(notify_error.__file__, encoding="utf-8") as handle:
            src = handle.read()
        self.assertNotIn("CHANNEL_ID", src)
        self.assertNotIn("TELEGRAM_CHAT_ID", src)
        self.assertNotIn("import common", src)
        self.assertNotIn("import post", src)
        self.assertNotIn("threading", src)


class GhaAlertTest(AlertCase):
    def _run(self, argv, plan, execute):
        buf = io.StringIO()
        with patch.object(gha_run, "plan", return_value=plan), \
             patch.object(gha_run, "execute", return_value=execute) as ran, \
             redirect_stdout(buf):
            rc = gha_run.main(argv)
        return rc, buf.getvalue(), ran

    def test_skip_and_quiet_success_do_not_alert(self):
        rc, out, ran = self._run(["tick"], ("skip", "tick"), (1, "should not run\n"))
        self.assertEqual(rc, 0)
        ran.assert_not_called()
        self.http.assert_not_called()
        self.assertNotIn("alert: delivered", _lines(out))
        self.assertFalse(os.path.exists(self.report))

        rc, out, ran = self._run(
            ["news"], ("run", "news"),
            (0, "[2026-11-07 10:51:00] nothing newsworthy\n"),
        )
        self.assertEqual(rc, 0)
        self.http.assert_not_called()
        self.assertFalse(os.path.exists(self.report))
        self.assertNotIn("alert:", out)

        rc, out, _ran = self._run(
            ["announce"], ("run", "announce"),
            (0, "no notable matches on 2026-11-07 (errors: []) -> nothing posted\n"),
        )
        self.assertEqual(rc, 0)
        self.http.assert_not_called()
        self.assertFalse(os.path.exists(self.report))

    def test_dry_run_failure_does_not_alert(self):
        rc, out, ran = self._run(
            ["announce", "--dry-run"], ("run", "announce"), (1, "FAILED\nABORT: dry-run failed.\n"),
        )
        self.assertEqual(rc, 1)
        ran.assert_called_once()
        self.http.assert_not_called()
        self.assertFalse(os.path.exists(self.report))
        self.assertIn("alert: dry-run, not sending", _lines(out))
        self.assertNotIn("alert: delivered", _lines(out))

    def test_publication_failure_alerts_once_and_keeps_the_exit_code(self):
        self._allow_mock_http()
        os.environ["GITHUB_ACTIONS"] = "true"
        os.environ["POSTING_ENABLED"] = "true"
        os.environ["GITHUB_SERVER_URL"] = "https://github.com"
        os.environ["GITHUB_REPOSITORY"] = "RustyIOS/de_csnews_bot"
        os.environ["GITHUB_RUN_ID"] = "999"
        buf = io.StringIO()
        with patch.object(gha_run, "plan", return_value=("run", "announce")), \
             patch.object(gha_run, "execute", return_value=(1, ANNOUNCE_LOG)), \
             redirect_stdout(buf):
            rc = gha_run.main(["announce"])
        self.assertEqual(rc, 1)
        self.assertEqual(self.http.call_count, 1)
        url, body = self._payload()
        self.assertTrue(url.endswith("/sendMessage"))
        self.assertEqual(body["chat_id"], 424242424)
        self.assertNotIn(CHANNEL_ID, json.dumps(body))
        self.assertIn("Этап: announce", body["text"])
        self.assertIn("Статус: не отправлено", body["text"])
        self.assertIn("Материал: Матчи дня, 7 ноября", body["text"])
        self.assertIn("Nothing sent", body["text"])
        self.assertNotIn("NAVI vs Spirit", body["text"])
        self.assertNotIn("UNIQUE_MIDDLE_LOG_LINE", body["text"])
        self.assertIn("https://github.com/RustyIOS/de_csnews_bot/actions/runs/999", body["text"])
        self.assertRegex(body["text"], r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} MSK")
        self.assertIn("alert: delivered", _lines(buf.getvalue()))
        with open(self.report, encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertTrue(report["delivered"])
        self.assertEqual(report["status"], "not_posted")
        self.assertNotIn(TOKEN, json.dumps(report))
        second = io.StringIO()
        with redirect_stdout(second):
            again = notify_error.main(["--report", self.report])
        self.assertEqual(again, 0)
        self.assertEqual(self.http.call_count, 1)
        self.assertIn("already delivered for this run; not sending again", second.getvalue())

    def test_alert_exception_does_not_hide_the_publication_failure(self):
        def boom(**_k):
            raise RuntimeError(f"leaked {TOKEN} https://api.telegram.org/bot{TOKEN}/sendMessage")

        buf = io.StringIO()
        with patch.object(gha_run, "plan", return_value=("run", "news")), \
             patch.object(gha_run, "execute", return_value=(1, "every source failed\n")), \
             patch("notify_error.alert_publication", side_effect=boom), \
             redirect_stdout(buf):
            rc = gha_run.main(["news"])
        out = buf.getvalue()
        self.assertEqual(rc, 1)
        self.http.assert_not_called()
        self.assertIn("alert: not delivered (RuntimeError)", _lines(out))
        self.assertNotIn(TOKEN, out)
        self.assertNotIn("alert: delivered", _lines(out))

    def test_http_failure_during_the_job_still_returns_the_publication_code(self):
        import requests
        os.environ["NOTIFY_FORBID_NETWORK"] = ""

        def boom(*_a, **_k):
            raise requests.Timeout(f"timed out https://api.telegram.org/bot{TOKEN}/sendMessage")

        self.http.side_effect = boom
        buf = io.StringIO()
        with patch.object(gha_run, "plan", return_value=("run", "results")), \
             patch.object(gha_run, "execute", return_value=(1, "ABORT: dry-run failed. Nothing sent.\n")), \
             redirect_stdout(buf):
            rc = gha_run.main(["results"])
        out = buf.getvalue()
        self.assertEqual(rc, 1)
        self.assertNotIn(TOKEN, out)
        self.assertTrue(any(line.startswith("alert: not delivered") for line in _lines(out)))
        self.assertNotIn("alert: delivered", _lines(out))
        with open(self.report, encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertFalse(report["delivered"])

    def test_live_gate_idle_is_quiet_and_fail_still_fails(self):
        with patch.object(gha_run, "call_gate", return_value=("IDLE", "IDLE\n")):
            rc, _text = gha_run.run_live(False)
        self.assertEqual(rc, 0)
        with patch.object(gha_run, "call_gate", return_value=("FAIL", "boom\n")):
            rc, text = gha_run.run_live(False)
        self.assertEqual(rc, 1)
        self.assertIn("live gate FAIL", text)
        self.http.assert_not_called()

    def test_preflight_does_not_execute_or_alert(self):
        with patch.object(gha_run, "plan", return_value=("run", "announce")), \
             patch.object(gha_run, "execute", side_effect=AssertionError("executed")):
            rc = gha_run.main(["announce", "--preflight"])
        self.assertEqual(rc, 0)
        self.http.assert_not_called()

    def test_workflow_and_state_sync_wire_the_private_alert(self):
        with open(os.path.join(ROOT, ".github", "workflows", "csnews.yml"), encoding="utf-8") as handle:
            workflow = handle.read()
        self.assertGreaterEqual(workflow.count("secrets.TELEGRAM_ALERT_CHAT_ID"), 2)
        self.assertIn("notify_error.py --report publish_failure.json", workflow)
        self.assertIn("continue-on-error: true", workflow)
        self.assertIn("steps.exec.outcome == 'failure'", workflow)
        self.assertIn("github.event.inputs.dry_run != 'true'", workflow)
        self.assertNotIn(CHANNEL_ID, workflow)
        self.assertNotIn("@de_csnews", workflow)
        with open(os.path.join(ROOT, "scripts", "state_sync.sh"), encoding="utf-8") as handle:
            sync = handle.read()
        self.assertIn("alert_dedup.json", sync)
        self.assertIn("add -f -- alert_dedup.json", sync)


if __name__ == "__main__":
    unittest.main()
