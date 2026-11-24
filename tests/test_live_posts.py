"""Live result posts: missing map scores, and a hold that does not fail the run.

bo3 often marks the decider map finished before winner_clan_score is filled.
That used to render as 'Ancient None:None'. The result stays pending until a
fresh bo3 fetch or Liquipedia has the score, or until about 60 minutes have
passed, and then the series score is posted without that map line.
"""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import check_post
import live_auto
import matches
from common import valid_score

NOW = datetime.datetime(2026, 11, 24, 9, 40, tzinfo=timezone.utc)


def _raw(winner=None, loser=None):
    return {
        "id": 134375,
        "status": "finished",
        "slug": "nip-vs-lynn-vision-24-11-2026",
        "bo_type": 1,
        "team1_id": 10,
        "team2_id": 20,
        "team1_score": 1,
        "team2_score": 0,
        "winner_team_id": 10,
        "start_date": "2026-11-24T08:00:00Z",
        "end_date": "2026-11-24T09:30:00Z",
        "team1": {"name": "Ninjas in Pyjamas"},
        "team2": {"name": "Lynn Vision"},
        "tournament": {"name": "PGL Major Singapore 2026 Stage 1"},
        "games": [{
            "number": 1,
            "status": "finished",
            "map_name": "de_ancient",
            "winner_clan_score": winner,
            "loser_clan_score": loser,
            "winner_team_clan": {"team_id": 10},
        }],
    }


def _match(end_minutes_ago=10, score=None):
    return {
        "key": "bo3:134375",
        "team1": "Ninjas in Pyjamas",
        "team2": "Lynn Vision",
        "score": "1:0",
        "status": "finished",
        "event": "PGL Major Singapore 2026 Stage 1",
        "format": "BO1",
        "url": "https://bo3.gg/matches/nip-vs-lynn-vision-24-11-2026",
        "end_utc": (NOW - datetime.timedelta(minutes=end_minutes_ago)).isoformat(),
        "maps": [{"map": "Ancient", "score": score, "number": 1}],
    }


def _finished(m):
    return {
        "type": "match_finished",
        "event_id": "match:finished",
        "id": "match:finished",
        "key": m["key"],
        "_m": m,
    }


class Bo3ScoreTest(unittest.TestCase):
    def test_missing_map_score_is_not_the_string_none(self):
        out = matches.bo3_norm(_raw())
        self.assertEqual(out["maps"][0]["map"], "Ancient")
        self.assertIsNone(out["maps"][0]["score"])
        self.assertNotIn("None", json.dumps(out["maps"]))
        self.assertEqual(out["score"], "1:0")

    def test_filled_map_score_is_team1_team2(self):
        out = matches.bo3_norm(_raw(13, 10))
        self.assertEqual(out["maps"][0]["score"], "13:10")
        self.assertIsNone(valid_score("None:None"))
        self.assertIsNone(valid_score(None))


class PrepareResultTest(unittest.TestCase):
    def test_fresh_bo3_fetch_fills_the_map(self):
        def boom(_raw):
            raise AssertionError("Liquipedia should wait until bo3 still has no score")

        status, ready = live_auto.prepare_finished(
            _match(score=None), NOW, fetch_match=lambda _mid: _raw(13, 10), fetch_lp=boom)
        self.assertEqual(status, "post")
        text, _template, card = live_auto.render_post(_finished(ready))
        self.assertIn("NiP 1:0 Lynn Vision", text)
        self.assertIn("Ancient 13:10", text)
        self.assertNotIn("None", text)
        self.assertEqual(card["maps"][0]["score"], "13:10")

    def test_liquipedia_fills_when_bo3_is_still_empty(self):
        status, ready = live_auto.prepare_finished(
            _match(score="None:None"), NOW,
            fetch_match=lambda _mid: _raw(),
            fetch_lp=lambda _raw: [{"map": "Ancient", "number": 1, "score": "13:9"}])
        self.assertEqual(status, "post")
        text, _template, _card = live_auto.render_post(_finished(ready))
        self.assertIn("Ancient 13:9", text)
        self.assertNotIn("None", text)

    def test_a_failed_fetch_holds_instead_of_crashing(self):
        def boom(*_args):
            raise RuntimeError("source down")

        def missing(_mid):
            raise SystemExit("bo3 match 134375 not found")

        status, _ready = live_auto.prepare_finished(
            _match(end_minutes_ago=5), NOW, fetch_match=missing, fetch_lp=boom)
        self.assertEqual(status, "hold")

    def test_recent_hole_is_held(self):
        status, ready = live_auto.prepare_finished(
            _match(end_minutes_ago=10), NOW,
            fetch_match=lambda _mid: _raw(), fetch_lp=lambda _raw: [])
        self.assertEqual(status, "hold")
        self.assertIsNone(ready["maps"][0]["score"])

    def test_after_an_hour_the_series_goes_out_without_the_map(self):
        status, ready = live_auto.prepare_finished(
            _match(end_minutes_ago=70), NOW,
            fetch_match=lambda _mid: _raw(), fetch_lp=lambda _raw: [])
        self.assertEqual(status, "post")
        text, _template, card = live_auto.render_post(_finished(ready))
        self.assertIn("NiP 1:0 Lynn Vision", text)
        self.assertNotIn("Ancient", text)
        self.assertNotIn("None", text)
        self.assertEqual(card["maps"], [])

    def test_hold_clock_starts_when_the_match_has_no_end(self):
        m = _match()
        m["end_utc"] = None
        status, _ready = live_auto.prepare_finished(
            m, NOW, fetch_match=lambda _mid: _raw(), fetch_lp=lambda _raw: [],
            hold_since=None)
        self.assertEqual(status, "hold")
        later = NOW + datetime.timedelta(minutes=61)
        status, ready = live_auto.prepare_finished(
            m, later, fetch_match=lambda _mid: _raw(), fetch_lp=lambda _raw: [],
            hold_since=(NOW - datetime.timedelta(minutes=61)).isoformat())
        self.assertEqual(status, "post")
        text, _template, _card = live_auto.render_post(_finished(ready))
        self.assertNotIn("None", text)
        self.assertIn("1:0", text)

    def test_render_never_prints_none_even_if_prepare_was_skipped(self):
        text, _template, card = live_auto.render_post(_finished(_match(score="None:None")))
        self.assertNotIn("None", text)
        self.assertNotIn("None", json.dumps(card))
        self.assertIn("1:0", text)


class HoldDoesNotBlockTest(unittest.TestCase):
    def test_hold_is_not_a_failure_and_the_next_post_still_goes(self):
        calls = []

        def send(ev, _dry):
            calls.append(ev["key"])
            return "hold" if ev["key"] == "bo3:1" else True

        posts = [{"key": "bo3:1"}, {"key": "bo3:2"}, {"key": "bo3:3"}]
        self.assertEqual(live_auto.run_due(posts, True, send, max_posts=2), 0)
        self.assertEqual(calls, ["bo3:1", "bo3:2", "bo3:3"])

    def test_a_real_failure_still_stops_the_run(self):
        calls = []

        def send(ev, _dry):
            calls.append(ev["key"])
            return "hold" if ev["key"] == "bo3:1" else False

        posts = [{"key": "bo3:1"}, {"key": "bo3:2"}, {"key": "bo3:3"}]
        self.assertEqual(live_auto.run_due(posts, True, send), 1)
        self.assertEqual(calls, ["bo3:1", "bo3:2"])

    def test_held_result_is_not_marked_sent(self):
        m = _match(end_minutes_ago=5, score=None)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"seen": {}, "live_events": {}}, fh)
            with patch("common.STATE_FILE", path), \
                 patch("live_auto._fetch_match", return_value=_raw()), \
                 patch("live_auto._fetch_lp", return_value=[]), \
                 patch("live_auto.check_and_post", side_effect=AssertionError("must not post")):
                status = live_auto.send_one(_finished(m), dry=False, now=NOW)
            self.assertEqual(status, "hold")
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
        row = saved["live_events"]["bo3:134375"]
        self.assertNotIn("match:finished", row.get("seen") or [])
        self.assertTrue(row.get("score_hold_since"))


class NoneScoreCheckTest(unittest.TestCase):
    def test_none_scores_fail_and_prose_does_not(self):
        self.assertTrue(check_post.none_score_failures("Ancient None:None"))
        self.assertTrue(check_post.none_score_failures("Dust2 None"))
        self.assertTrue(check_post.none_score_failures("карта 2 None"))
        self.assertTrue(check_post.none_score_failures("NiP 13:None Lynn Vision"))
        self.assertTrue(check_post.none_score_failures("NiP None:8 Lynn Vision"))
        self.assertEqual(check_post.none_score_failures("None of the players stayed"), [])
        self.assertEqual(check_post.none_score_failures("Ancient 13:10"), [])

    def _draft(self, map_line):
        return (
            "<b>NiP 1:0 Lynn Vision</b>\n\n"
            "🏆 PGL Major\n"
            f"{map_line}\n\n"
            "#CS2 #Major\n"
        )

    def test_check_post_exits_on_none(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.txt")
            good = os.path.join(tmp, "good.txt")
            with open(bad, "w", encoding="utf-8") as fh:
                fh.write(self._draft("Ancient None:None"))
            with open(good, "w", encoding="utf-8") as fh:
                fh.write(self._draft("Ancient 13:10"))
            failed = subprocess.run(
                [sys.executable, "check_post.py", bad, "--offline"],
                cwd=root, capture_output=True, text=True)
            ok = subprocess.run(
                [sys.executable, "check_post.py", good, "--offline"],
                cwd=root, capture_output=True, text=True)
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        self.assertIn("None:None", failed.stdout)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)


if __name__ == "__main__":
    unittest.main()
