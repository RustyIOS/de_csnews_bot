"""Morning announce must not eat the 10:51 news slot.

A day with nothing notable is recorded in announced_dates. A fetch or send
error is not. When both are due, the news slot takes the tick.
"""
import datetime
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import announce_auto
import scripts.gha_run as gha_run
from common import MSK

DAY = "2026-11-01"
SLOT = "2026-11-01T10:51"
MATCH = {
    "status": "upcoming",
    "event": "BLAST Premier",
    "team1": "Natus Vincere",
    "team2": "Team Spirit",
    "start_msk": "18:00",
    "format": "bo3",
    "stage": "Group A",
    "tier": "S",
    "key": "bo3:999001",
    "url": "https://bo3.gg/matches/999001",
}


def at(hour, minute):
    return datetime.datetime(2026, 11, 1, hour, minute, tzinfo=MSK)


class AnnounceTickTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auto_path = os.path.join(self.tmp.name, "auto_state.json")
        self._cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self._cwd)
        state = patch("auto_lib.STATE", self.auto_path)
        state.start()
        self.addCleanup(state.stop)
        self._write_state({
            "announced_dates": ["2026-10-17"],
            "recapped_keys": ["bo3:131673"],
            "results_dates": ["2026-10-17", "2026-10-31"],
            "results_note": "kept as on the state branch",
            "news_slots": ["2026-10-31T16:51"],
        })

    def _write_state(self, st):
        with open(self.auto_path, "w", encoding="utf-8") as f:
            json.dump(st, f)

    def _state(self):
        with open(self.auto_path, encoding="utf-8") as f:
            return json.load(f)

    def test_no_matches_day_marks_done_and_next_tick_runs_news(self):
        with patch("announce_auto.single_instance", return_value=object()), \
             patch("announce_auto.run_json", return_value={"matches": [], "errors": []}) as fetch:
            rc = announce_auto.main(["--date", DAY])
        self.assertEqual(rc, 0)
        fetch.assert_called_once()
        st = self._state()
        self.assertEqual(st["announced_dates"], ["2026-10-17", DAY])
        self.assertEqual(st["recapped_keys"], ["bo3:131673"])
        self.assertEqual(st["results_dates"], ["2026-10-17", "2026-10-31"])
        self.assertEqual(st["results_note"], "kept as on the state branch")
        self.assertEqual(st["news_slots"], ["2026-10-31T16:51"])
        # 10:50 is still before the news window; announce is already settled.
        self.assertEqual(gha_run.tick_choice(at(10, 50), st), "live")
        self.assertEqual(gha_run.tick_choice(at(10, 51), st), "news")
        self.assertEqual(gha_run.tick_choice(at(11, 0), st), "news")
        self.assertEqual(gha_run.news_slot_id(at(10, 51)), SLOT)

    def test_fetch_error_and_send_error_leave_the_day_open(self):
        with patch("announce_auto.single_instance", return_value=object()), \
             patch("announce_auto.run_json", return_value={"matches": [], "errors": ["bo3.gg: HTTP 500"]}):
            rc = announce_auto.main(["--date", DAY])
        self.assertEqual(rc, 1)
        self.assertEqual(self._state()["announced_dates"], ["2026-10-17"])
        self.assertEqual(gha_run.tick_choice(at(10, 20), self._state()), "announce")

        with patch("announce_auto.single_instance", return_value=object()), \
             patch("announce_auto.run_json", return_value={"matches": [MATCH], "errors": []}), \
             patch("announce_auto.run", return_value=(1, "", "card failed")), \
             patch("announce_auto.check_and_post", return_value=False):
            rc = announce_auto.main(["--date", DAY])
        self.assertEqual(rc, 1)
        self.assertEqual(self._state()["announced_dates"], ["2026-10-17"])
        self.assertEqual(gha_run.tick_choice(at(10, 30), self._state()), "announce")
        # The send failure must not consume the news window either.
        self.assertEqual(gha_run.tick_choice(at(11, 0), self._state()), "news")
        self.assertEqual(gha_run.tick_choice(at(11, 20), self._state()), "announce")

    def test_dry_run_empty_does_not_mark(self):
        with patch("announce_auto.single_instance", return_value=object()), \
             patch("announce_auto.run_json", return_value={"matches": [], "errors": []}):
            rc = announce_auto.main(["--dry-run", "--date", DAY])
        self.assertEqual(rc, 0)
        self.assertEqual(self._state()["announced_dates"], ["2026-10-17"])

    def test_news_slot_due_while_announce_pending(self):
        st = {"announced_dates": ["2026-10-17"], "news_slots": [], "results_dates": []}
        self.assertEqual(gha_run.tick_choice(at(10, 50), st), "announce")
        self.assertEqual(gha_run.tick_choice(at(10, 51), st), "news")
        self.assertEqual(gha_run.tick_choice(at(11, 0), st), "news")
        self.assertEqual(gha_run.tick_choice(at(11, 11), st), "news")
        served = dict(st, news_slots=[SLOT])
        self.assertEqual(gha_run.tick_choice(at(11, 10), served), "announce")
        self.assertEqual(gha_run.tick_choice(at(11, 12), st), "announce")

    def test_normal_announce_posts_once(self):
        pending = self._state()
        self.assertEqual(gha_run.tick_choice(at(10, 10), pending), "announce")
        with patch("announce_auto.single_instance", return_value=object()), \
             patch("announce_auto.run_json", return_value={"matches": [MATCH], "errors": []}) as fetch, \
             patch("announce_auto.run", return_value=(1, "", "card failed")) as run, \
             patch("announce_auto.check_and_post", return_value=True) as post:
            rc = announce_auto.main(["--date", DAY])
            self.assertEqual(rc, 0)
            self.assertEqual(post.call_args[0][0], f"ann-{DAY}")
            post.assert_called_once()
            rc = announce_auto.main(["--date", DAY])
        self.assertEqual(rc, 0)
        fetch.assert_called_once()
        post.assert_called_once()
        # Card render, then matches.py --mark. The second run does not post.
        self.assertEqual(run.call_count, 2)
        st = self._state()
        self.assertEqual(st["announced_dates"], ["2026-10-17", DAY])
        self.assertEqual(st["announced_dates"].count(DAY), 1)
        self.assertEqual(st["news_slots"], ["2026-10-31T16:51"])
        self.assertEqual(gha_run.tick_choice(at(10, 20), st), "live")

    def test_other_windows_stay_put(self):
        announced = {
            "announced_dates": [DAY],
            "news_slots": [],
            "results_dates": [],
        }
        self.assertEqual(gha_run.tick_choice(at(13, 51), announced), "news")
        # 14:00 is still inside the 13:51 slot's 20-minute catch-up.
        self.assertEqual(gha_run.tick_choice(at(14, 12), announced), "live")
        self.assertEqual(gha_run.tick_choice(at(23, 40), announced), "results")
        self.assertIsNone(gha_run.tick_choice(at(9, 30), announced))
        served_news = dict(announced, news_slots=["2026-11-01T13:51"])
        self.assertEqual(gha_run.tick_choice(at(13, 55), served_news), "live")
        done = dict(announced, results_dates=[DAY])
        self.assertEqual(gha_run.tick_choice(at(23, 50), done), "live")

    def test_announced_dates_keep_the_last_60(self):
        dates = [f"2026-08-{d:02d}" for d in range(1, 31)]
        dates += [f"2026-09-{d:02d}" for d in range(1, 31)]
        self.assertEqual(len(dates), 60)
        self._write_state({"announced_dates": dates, "recapped_keys": [], "results_dates": []})
        announce_auto.note_announced(DAY)
        got = self._state()["announced_dates"]
        self.assertEqual(len(got), 60)
        self.assertNotIn("2026-08-01", got)
        self.assertEqual(got[-1], DAY)


if __name__ == "__main__":
    unittest.main()
