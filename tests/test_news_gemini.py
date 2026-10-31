"""Gemini drafts the news post; OpenRouter runs only when Gemini cannot."""
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_auto

KEY = "test-gemini-key-not-real"
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
    "GEMINI_API_KEY": KEY,
    "OPENROUTER_API_KEY": "test-openrouter-key",
    "OPENROUTER_MODELS": "model-a,model-b",
}


class GeminiResp:
    def __init__(self, status, content="", finish="STOP", raw=None, parts=None):
        self.status_code = status
        self.text = content if raw is None else raw
        if parts is None:
            parts = [{"text": content}]
        self._body = {
            "candidates": [{
                "finishReason": finish,
                "content": {"role": "model", "parts": parts},
            }]
        }

    def json(self):
        return self._body


class OpenRouterResp:
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


class GeminiChatTest(unittest.TestCase):
    def _route(self, script):
        """script items: (provider, model, response-or-exception) in call order."""
        calls = []
        pending = list(script)

        def post(url, headers=None, json=None, timeout=None):
            provider, model, action = pending.pop(0)
            calls.append({
                "url": url,
                "headers": headers or {},
                "json": json,
                "timeout": timeout,
                "model": model,
                "provider": provider,
            })
            if provider == "gemini":
                self.assertIn(f"/models/{model}:generateContent", url)
                self.assertNotIn(KEY, url)
                self.assertNotIn("key=", url)
                self.assertEqual((headers or {}).get("x-goog-api-key"), KEY)
                self.assertNotIn("Authorization", headers or {})
                self.assertEqual(timeout, news_auto.GEMINI_TIMEOUT)
                cfg = json["generationConfig"]
                self.assertEqual(cfg["responseMimeType"], "application/json")
                self.assertEqual(json["systemInstruction"]["parts"][0]["text"], news_auto.SYSTEM_PROMPT)
            elif provider == "openrouter":
                self.assertIn("openrouter.ai", url)
                self.assertEqual(json["model"], model)
                self.assertNotIn(KEY, url)
            else:
                self.fail(provider)
            if isinstance(action, BaseException):
                raise action
            return action

        return calls, post

    def _logs(self, log):
        return "\n".join(c.args[0] for c in log.call_args_list)

    def test_success(self):
        env = dict(ENV, GEMINI_MODELS="gemini-3.5-flash")
        calls, post = self._route([
            ("gemini", "gemini-3.5-flash", GeminiResp(200, GOOD)),
        ])
        with patch.dict(os.environ, env, clear=False), \
                patch("requests.post", side_effect=post), \
                patch("news_auto.log") as log:
            raw = news_auto.chat("Материалы:")
        self.assertEqual(news_auto.parse_model_posts(raw)[0]["id"], "tg:cs3news:18900")
        self.assertEqual([c["provider"] for c in calls], ["gemini"])
        self.assertEqual(calls[0]["timeout"], 60)
        self.assertEqual(len(calls[0]["json"]["contents"]), 1)
        self.assertEqual(calls[0]["json"]["contents"][0]["role"], "user")
        text = self._logs(log)
        self.assertIn("Gemini draft from gemini-3.5-flash", text)
        self.assertNotIn(KEY, text)
        self.assertNotIn("OpenRouter", text)

    def test_thought_part_is_ignored(self):
        env = dict(ENV, GEMINI_MODELS="gemini-3.5-flash")
        calls, post = self._route([
            ("gemini", "gemini-3.5-flash", GeminiResp(200, parts=[
                {"text": "сначала отберу факты", "thought": True},
                {"text": GOOD},
            ])),
        ])
        with patch.dict(os.environ, env, clear=False), patch("requests.post", side_effect=post):
            raw = news_auto.chat("Материалы:")
        self.assertEqual(news_auto.parse_model_posts(raw)[0]["id"], "tg:cs3news:18900")
        self.assertNotIn("сначала отберу", raw)
        self.assertEqual(len(calls), 1)

    def test_bad_json_nudges_then_next_model(self):
        env = dict(ENV, GEMINI_MODELS="gemini-a,gemini-b")
        calls, post = self._route([
            ("gemini", "gemini-a", GeminiResp(200, TRUNC, finish="MAX_TOKENS")),
            ("gemini", "gemini-a", GeminiResp(200, TRUNC, finish="MAX_TOKENS")),
            ("gemini", "gemini-b", GeminiResp(200, GOOD)),
        ])
        with patch.dict(os.environ, env, clear=False), \
                patch("requests.post", side_effect=post), \
                patch("news_auto.log") as log:
            raw = news_auto.chat("Материалы:")
        self.assertEqual(news_auto.parse_model_posts(raw)[0]["id"], "tg:cs3news:18900")
        self.assertEqual([c["model"] for c in calls], ["gemini-a", "gemini-a", "gemini-b"])
        self.assertEqual(len(calls[0]["json"]["contents"]), 1)
        nudged = calls[1]["json"]["contents"]
        self.assertEqual([c["role"] for c in nudged], ["user", "model", "user"])
        self.assertTrue(nudged[1]["parts"][0]["text"].startswith('{"posts":'))
        self.assertEqual(nudged[2]["parts"][0]["text"], news_auto.JSON_NUDGE)
        # The next model sees the original prompt, not the failed turn.
        self.assertEqual(len(calls[2]["json"]["contents"]), 1)
        text = self._logs(log)
        self.assertIn("Gemini draft from gemini-b", text)
        self.assertNotIn(KEY, text)
        self.assertNotIn("trying OpenRouter", text)

    def test_empty_posts_does_not_fall_through(self):
        env = dict(ENV, GEMINI_MODELS="gemini-3.5-flash")
        calls, post = self._route([
            ("gemini", "gemini-3.5-flash", GeminiResp(200, '{"posts":[]}')),
        ])
        with patch.dict(os.environ, env, clear=False), patch("requests.post", side_effect=post):
            raw = news_auto.chat("Материалы:")
        self.assertEqual(news_auto.parse_model_posts(raw), [])
        self.assertEqual(len(calls), 1)

    def test_missing_or_blank_key_uses_openrouter_only(self):
        for blank in (None, "", "   "):
            with self.subTest(blank=blank):
                calls, post = self._route([
                    ("openrouter", "model-a", OpenRouterResp(200, GOOD)),
                ])
                env = {
                    "OPENROUTER_API_KEY": "test-key",
                    "OPENROUTER_MODELS": "model-a,model-b",
                }
                if blank is not None:
                    env["GEMINI_API_KEY"] = blank
                with patch.dict(os.environ, env, clear=False), \
                        patch("requests.post", side_effect=post):
                    if blank is None:
                        os.environ.pop("GEMINI_API_KEY", None)
                    raw = news_auto.chat("Материалы:")
                self.assertEqual(news_auto.parse_model_posts(raw)[0]["id"], "tg:cs3news:18900")
                self.assertEqual([c["provider"] for c in calls], ["openrouter"])
                body = calls[0]["json"]
                self.assertEqual(body["model"], "model-a")
                self.assertEqual(body["temperature"], 0.3)
                self.assertEqual(body["max_tokens"], 1000)
                self.assertEqual(
                    [m["role"] for m in body["messages"]],
                    ["system", "user"],
                )
                self.assertEqual(body["messages"][0]["content"], news_auto.SYSTEM_PROMPT)

    def test_503_falls_back(self):
        env = dict(ENV)
        env.pop("GEMINI_MODELS", None)
        calls, post = self._route([
            ("gemini", "gemini-3.5-flash", GeminiResp(503, raw="high demand")),
            ("gemini", "gemini-3.1-flash-lite", GeminiResp(503, raw="high demand")),
            ("openrouter", "model-a", OpenRouterResp(200, GOOD)),
        ])
        with patch.dict(os.environ, env, clear=False), \
                patch("requests.post", side_effect=post), \
                patch("news_auto.time.sleep") as sleep, \
                patch("news_auto.log") as log:
            os.environ.pop("GEMINI_MODELS", None)
            raw = news_auto.chat("Материалы:")
        sleep.assert_not_called()
        self.assertEqual(news_auto.parse_model_posts(raw)[0]["id"], "tg:cs3news:18900")
        self.assertEqual(
            [c["model"] for c in calls],
            ["gemini-3.5-flash", "gemini-3.1-flash-lite", "model-a"],
        )
        # A 503 is not a bad draft: no JSON nudge, one request per model.
        self.assertEqual(len(calls[0]["json"]["contents"]), 1)
        self.assertEqual(len(calls[1]["json"]["contents"]), 1)
        text = self._logs(log)
        self.assertIn("Gemini gemini-3.5-flash: HTTP 503", text)
        self.assertIn("Gemini gemini-3.1-flash-lite: HTTP 503", text)
        self.assertIn("Gemini produced no draft; trying OpenRouter", text)
        self.assertIn("OpenRouter draft from model-a", text)
        self.assertNotIn(KEY, text)
        self.assertNotIn("high demand", text)

    def test_timeout_tries_next_model(self):
        import requests
        env = dict(ENV)
        env.pop("GEMINI_MODELS", None)
        calls, post = self._route([
            ("gemini", "gemini-3.5-flash", requests.Timeout("read timed out")),
            ("gemini", "gemini-3.1-flash-lite", GeminiResp(200, GOOD)),
        ])
        with patch.dict(os.environ, env, clear=False), \
                patch("requests.post", side_effect=post), \
                patch("news_auto.log") as log:
            os.environ.pop("GEMINI_MODELS", None)
            raw = news_auto.chat("Материалы:")
        self.assertEqual(news_auto.parse_model_posts(raw)[0]["id"], "tg:cs3news:18900")
        self.assertEqual(
            [c["model"] for c in calls],
            ["gemini-3.5-flash", "gemini-3.1-flash-lite"],
        )
        text = self._logs(log)
        self.assertIn("Gemini gemini-3.5-flash: timeout", text)
        self.assertIn("Gemini draft from gemini-3.1-flash-lite", text)
        self.assertNotIn(KEY, text)
        self.assertNotIn("read timed out", text)


if __name__ == "__main__":
    unittest.main()
