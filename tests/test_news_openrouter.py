"""OpenRouter news drafts: a bad first reply must not end the slot."""
import datetime
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_auto
from common import MSK

# The 2026-10-26 news run died on a 128-char cut of this shape.
TRUNC = (
    '{"posts":[{"id":"hltv:hltvnews45809","text":"'
    "<b>Imperial выиграли Circuit X Curitiba</b>\\n\\nImperial победили на Circuit X Cur"
)
GOOD = json.dumps(
    {
        "posts": [{
            "id": "tg:cs3news:18900",
            "text": (
                "<b>ENCE объявили о банкротстве</b>\n\n"
                "Организация сообщила о банкротстве.\n\n"
                "#CS2 #ENCE #de_csnews"
            ),
        }]
    },
    ensure_ascii=False,
)
ENV = {
    "OPENROUTER_API_KEY": "test-key",
    "OPENROUTER_MODELS": "model-a,model-b",
}


class FakeResp:
    def __init__(self, status, content="", finish="stop", raw=None):
        self.status_code = status
        self.text = content if raw is None else raw
        self._body = {
            "choices": [{
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content},
            }]
        }

    def json(self):
        return self._body


class ParseModelPostsTest(unittest.TestCase):
    def test_plain_and_trailing_junk(self):
        posts = news_auto.parse_model_posts(GOOD + "\nНадеюсь, подошло.")
        self.assertEqual(posts[0]["id"], "tg:cs3news:18900")

    def test_fence_after_a_decoy_brace(self):
        text = "Схема {не json}.\n```json\n" + GOOD + "\n```\n"
        posts = news_auto.parse_model_posts(text)
        self.assertEqual(posts[0]["id"], "tg:cs3news:18900")

    def test_empty_posts_list(self):
        self.assertEqual(news_auto.parse_model_posts('{"posts":[]}'), [])

    def test_truncated_json_is_rejected(self):
        self.assertIsNone(news_auto.parse_model_posts(TRUNC))

    def test_posts_must_be_a_list(self):
        self.assertIsNone(news_auto.parse_model_posts('{"posts":"nope"}'))


class ChatFallbackTest(unittest.TestCase):
    def setUp(self):
        # A developer key must not pull these cases onto the Gemini path.
        self._gemini = os.environ.pop("GEMINI_API_KEY", None)
        self.addCleanup(self._restore_gemini)

    def _restore_gemini(self):
        if self._gemini is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = self._gemini

    def _post(self, script):
        """script: list of (model, response) consumed in order."""
        bodies = []
        pending = list(script)

        def post(url, headers=None, json=None, timeout=None):
            bodies.append(json)
            model, resp = pending.pop(0)
            self.assertEqual(json["model"], model)
            return resp

        return bodies, post

    def test_truncated_first_model_falls_through(self):
        bodies, post = self._post([
            ("model-a", FakeResp(200, TRUNC, finish="length")),
            ("model-a", FakeResp(200, TRUNC, finish="length")),
            ("model-b", FakeResp(200, GOOD)),
        ])
        with patch.dict(os.environ, ENV, clear=False), patch("requests.post", side_effect=post):
            raw = news_auto.chat("Материалы:")
        self.assertEqual(news_auto.parse_model_posts(raw)[0]["id"], "tg:cs3news:18900")
        self.assertNotEqual(raw, TRUNC)
        self.assertEqual([b["model"] for b in bodies], ["model-a", "model-a", "model-b"])
        self.assertEqual(len(bodies[0]["messages"]), 2)
        self.assertEqual(
            [m["role"] for m in bodies[1]["messages"]],
            ["system", "user", "assistant", "user"],
        )
        self.assertTrue(bodies[1]["messages"][2]["content"].startswith('{"posts":'))
        self.assertIn('{"posts":[]}', bodies[1]["messages"][3]["content"])
        # The next model sees the original prompt, not the failed turn.
        self.assertEqual(len(bodies[2]["messages"]), 2)

    def test_nudge_on_the_same_model_is_enough(self):
        bodies, post = self._post([
            ("model-a", FakeResp(200, TRUNC, finish="length")),
            ("model-a", FakeResp(200, GOOD)),
        ])
        with patch.dict(os.environ, ENV, clear=False), patch("requests.post", side_effect=post):
            raw = news_auto.chat("Материалы:")
        self.assertEqual(news_auto.parse_model_posts(raw)[0]["id"], "tg:cs3news:18900")
        self.assertEqual([b["model"] for b in bodies], ["model-a", "model-a"])

    def test_empty_posts_stops_the_fallback(self):
        bodies, post = self._post([
            ("model-a", FakeResp(200, '{"posts":[]}')),
        ])
        with patch.dict(os.environ, ENV, clear=False), patch("requests.post", side_effect=post):
            raw = news_auto.chat("Материалы:")
        self.assertEqual(news_auto.parse_model_posts(raw), [])
        self.assertEqual(len(bodies), 1)

    def test_every_model_bad_returns_none(self):
        bodies, post = self._post([
            ("model-a", FakeResp(200, TRUNC)),
            ("model-a", FakeResp(200, "sure, here are some posts")),
            ("model-b", FakeResp(200, TRUNC)),
            ("model-b", FakeResp(200, "")),
        ])
        with patch.dict(os.environ, ENV, clear=False), patch("requests.post", side_effect=post):
            raw = news_auto.chat("Материалы:")
        self.assertIsNone(raw)
        self.assertEqual([b["model"] for b in bodies], ["model-a", "model-a", "model-b", "model-b"])

    def test_http_error_skips_the_nudge(self):
        bodies, post = self._post([
            ("model-a", FakeResp(404, raw="no such model")),
            ("model-b", FakeResp(200, GOOD)),
        ])
        with patch.dict(os.environ, ENV, clear=False), patch("requests.post", side_effect=post):
            raw = news_auto.chat("Материалы:")
        self.assertEqual(news_auto.parse_model_posts(raw)[0]["id"], "tg:cs3news:18900")
        self.assertEqual([b["model"] for b in bodies], ["model-a", "model-b"])

    def test_429_retries_without_the_json_nudge(self):
        bodies, post = self._post([
            ("model-a", FakeResp(429, raw="slow down")),
            ("model-a", FakeResp(200, GOOD)),
        ])
        with patch.dict(os.environ, ENV, clear=False), \
                patch("requests.post", side_effect=post), \
                patch("news_auto.time.sleep") as sleep:
            raw = news_auto.chat("Материалы:")
        sleep.assert_called_once_with(5)
        self.assertEqual(news_auto.parse_model_posts(raw)[0]["id"], "tg:cs3news:18900")
        self.assertEqual([b["model"] for b in bodies], ["model-a", "model-a"])
        self.assertEqual(len(bodies[1]["messages"]), 2)


# 2026-10-28 16:51 MSK: the only chosen draft failed normalize_draft, the run
# still closed the slot, and the story was mark_skipped.
SLOT = "2026-10-28T16:51"
FAIL_ID = "hltv:hltvnews45838"
OTHER_ID = "hltv:hltvnews45800"
JUNK_ID = "reddit:meme1"
ENGLISH = (
    "<b>Legacy bench ksloks</b>\n\n"
    "Legacy released ksloks. The roster move is official.\n\n"
    "#CS2 #Legacy #de_csnews"
)
RUSSIAN = (
    "<b>ENCE объявили о банкротстве</b>\n\n"
    "Организация сообщила о банкротстве. Игроки ищут новый состав.\n\n"
    "#CS2 #ENCE #de_csnews"
)


def _item(iid, title):
    return {"id": iid, "title": title, "source": "HLTV", "url": f"https://example.test/{iid}"}


class NormalizeDraftReasonTest(unittest.TestCase):
    def _why(self, text):
        with patch("news_auto.log") as log:
            self.assertIsNone(news_auto.normalize_draft(text))
        return " ".join(c.args[0] for c in log.call_args_list)

    def test_empty(self):
        self.assertIn("empty", self._why(""))

    def test_fewer_than_two_sentences(self):
        text = "<b>Состав</b>\n\nОрганизация отпустила игрока\n\n#CS2 #Новости"
        self.assertIn("fewer than 2 sentences", self._why(text))

    def test_cyrillic_share(self):
        self.assertIn("cyrillic share", self._why(ENGLISH))

    def test_visible_chars(self):
        body = ("А" * 500) + ". " + ("А" * 500) + "."
        text = f"<b>Заголовок</b>\n\n{body}\n\n#CS2 #Новости"
        self.assertIn("visible chars > 900", self._why(text))


class FormatFailSlotTest(unittest.TestCase):
    """A parsed reply that publishes nothing must not burn the slot."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = os.path.join(self.tmp.name, "state.json")
        self.auto_path = os.path.join(self.tmp.name, "auto_state.json")
        when = datetime.datetime(2026, 10, 28, 16, 55, tzinfo=MSK)
        for p in (
            patch("common.STATE_FILE", self.state_path),
            patch("auto_lib.STATE", self.auto_path),
            patch("news_auto.now_msk", return_value=when),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.assertEqual(news_auto.news_slot_id(news_auto.now_msk()), SLOT)

    def _seen(self):
        if not os.path.exists(self.state_path):
            return {}
        with open(self.state_path, encoding="utf-8") as f:
            return json.load(f).get("seen") or {}

    def _slots(self):
        if not os.path.exists(self.auto_path):
            return []
        with open(self.auto_path, encoding="utf-8") as f:
            return json.load(f).get("news_slots") or []

    def test_all_drafts_format_fail_leaves_slot_and_item_unseen(self):
        self.assertIsNone(news_auto.normalize_draft(ENGLISH))
        failed = _item(FAIL_ID, "Legacy / ksloks")
        other = _item(OTHER_ID, "Other topic")
        junk = _item(JUNK_ID, "meme")
        parsed = [{"id": FAIL_ID, "text": ENGLISH}]
        with patch("news_auto.publish") as publish:
            rc = news_auto.publish_parsed([failed, other], [junk], parsed, dry=False)
        publish.assert_not_called()
        self.assertEqual(rc, 1)
        self.assertNotIn(SLOT, self._slots())
        seen = self._seen()
        self.assertNotIn(FAIL_ID, seen)
        self.assertEqual(seen[OTHER_ID]["status"], "skipped")
        self.assertEqual(seen[JUNK_ID]["status"], "skipped")

    def test_all_publish_fail_leaves_slot_and_item_unseen(self):
        self.assertIsNotNone(news_auto.normalize_draft(RUSSIAN))
        item = _item("tg:cs3news:18900", "ENCE")
        with patch("news_auto.publish", return_value=False) as publish:
            rc = news_auto.publish_parsed(
                [item], [], [{"id": item["id"], "text": RUSSIAN}], dry=False)
        publish.assert_called_once()
        self.assertEqual(rc, 1)
        self.assertNotIn(SLOT, self._slots())
        self.assertNotIn(item["id"], self._seen())

    def test_empty_posts_marks_the_slot(self):
        other = _item(OTHER_ID, "Other topic")
        junk = _item(JUNK_ID, "meme")
        rc = news_auto.publish_parsed([other], [junk], [], dry=False)
        self.assertEqual(rc, 0)
        self.assertIn(SLOT, self._slots())
        seen = self._seen()
        self.assertEqual(seen[OTHER_ID]["status"], "skipped")
        self.assertEqual(seen[JUNK_ID]["status"], "skipped")

    def test_one_send_marks_slot_but_format_fail_stays_unseen(self):
        failed = _item(FAIL_ID, "Legacy / ksloks")
        sent = _item("tg:cs3news:18900", "ENCE")
        other = _item(OTHER_ID, "Other topic")
        parsed = [
            {"id": FAIL_ID, "text": ENGLISH},
            {"id": sent["id"], "text": RUSSIAN},
        ]
        with patch("news_auto.publish", return_value=True) as publish:
            rc = news_auto.publish_parsed([failed, sent, other], [], parsed, dry=False)
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(rc, 0)
        self.assertIn(SLOT, self._slots())
        seen = self._seen()
        self.assertNotIn(FAIL_ID, seen)
        self.assertNotIn(sent["id"], seen)
        self.assertEqual(seen[OTHER_ID]["status"], "skipped")

    def test_unmatched_ids_do_not_skip_the_pool(self):
        other = _item(OTHER_ID, "Other topic")
        junk = _item(JUNK_ID, "meme")
        rc = news_auto.publish_parsed(
            [other], [junk], [{"id": "no-such", "text": ENGLISH}], dry=False)
        self.assertEqual(rc, 1)
        self.assertNotIn(SLOT, self._slots())
        self.assertEqual(self._seen(), {})


if __name__ == "__main__":
    unittest.main()
