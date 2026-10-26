"""OpenRouter news drafts: a bad first reply must not end the slot."""
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_auto

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


if __name__ == "__main__":
    unittest.main()
