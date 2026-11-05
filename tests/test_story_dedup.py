"""Same story from two sources must not be posted twice.

2026-11-05: Dust2 at 11:02 and Reddit at 17:04 both ran the 1win / Rolls-Royce
Major promise. The CS2 patch (Steam, then a cs3news tip) and the FISSURE debt
note (HLTV on Nov 4, acenewscs on Nov 5) did the same. English titles were
well under the old 0.85 cutoff, and post.py only looked back 3 hours.
"""
import datetime
import difflib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import CHANNEL_ID, MSK, norm_title, similar
import news_auto
import post
import story_dedup
from story_dedup import (
    NEWS_WINDOW_HOURS, best_match, content_tokens, digest_topics, gemini_same_story,
    parse_yes_no, story_relation,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = datetime.datetime(2026, 11, 5, 17, 4, tzinfo=MSK)

# Channel headlines from daily/2026-11-04.md and daily/2026-11-05.md.
DAY = [
    "Все участники PGL Major Singapore 2026 определены",
    "donk получил больше 1000 репортов на FACEIT",
    "Falcons попрощались с академией",
    "KSCERATO играет с выкрученной насыщенностью",
    "Bracket IEM Beijing определён",
    "The MongolZ в полуфинале",
    "Знакомьтесь: hitori из G2 Ares",
    "FISSURE выплатила половину долгов в CS2",
    "zonic о главных проблемах Falcons",
    "Новый патч в CS2",
    "Rolls-Royce за победу на мейджоре",
    "Автовыбор серверов на FACEIT",
    "Вышло обновление CS2",
    "FISSURE погасили около половины долгов",
    "Magnojez остается в BetBoom Team",
    "Анонсирован список талантов на BLAST Rivals Season 2",
    "Владелец 1win пообещал игрокам Rolls-Royce за победу на мейджоре",
]
MUST = {
    frozenset(("FISSURE выплатила половину долгов в CS2", "FISSURE погасили около половины долгов")),
    frozenset(("Новый патч в CS2", "Вышло обновление CS2")),
    frozenset(("Rolls-Royce за победу на мейджоре", "Владелец 1win пообещал игрокам Rolls-Royce за победу на мейджоре")),
}
DUST = "1win, BC.Game promise players Rolls-Royces if they win the Singapore Major"
# Permalink slug: 1win_team_qualified_for_their_first_major_and_the
# The channel headline of that post names Rolls-Royce, so the title below is those two together.
REDDIT = "1win team qualified for their first Major and the owner promised them Rolls-Royces"
SLUG = "1win team qualified for their first Major and the"
FISSURE_EN = "FISSURE CEO: around half of our CS2 debt has already been paid"
ROLLS_SHORT = "Rolls-Royce за победу на мейджоре"
ROLLS_LONG = "Владелец 1win пообещал игрокам Rolls-Royce за победу на мейджоре"
PATCH_A = "Новый патч в CS2"
PATCH_B = "Вышло обновление CS2"
FISSURE_A = "FISSURE выплатила половину долгов в CS2"
FISSURE_B = "FISSURE погасили около половины долгов"


def _no_tie(a, b):
    raise AssertionError(f"tie-break called for a clear pair: {a!r} / {b!r}")


def _item(iid, source, title, url):
    return {"id": iid, "source": source, "title": title, "url": url, "published": "2026-11-05 11:00 MSK"}


def _html(headline):
    return f"<b>{headline}</b>\n\nФакт один. Факт два.\n\n#CS2 #de_csnews"


class RelationTest(unittest.TestCase):
    def test_old_title_similarity_misses_the_rolls_pair(self):
        ratio = difflib.SequenceMatcher(None, norm_title(DUST), norm_title(REDDIT)).ratio()
        self.assertFalse(similar(DUST, REDDIT))
        self.assertLess(ratio, 0.85)
        self.assertGreater(ratio, 0.2)

    def test_published_headlines_block_only_the_real_repeats(self):
        for i, left in enumerate(DAY):
            for right in DAY[i + 1:]:
                rel = story_relation(left, right)
                if frozenset((left, right)) in MUST:
                    self.assertEqual(rel, "yes", f"{left} || {right} tokens {content_tokens(left)} {content_tokens(right)}")
                else:
                    self.assertNotEqual(rel, "yes", f"false block {left} || {right}")

    def test_rolls_royce_sources_match_the_published_headline(self):
        for title in (DUST, REDDIT, ROLLS_LONG):
            self.assertEqual(story_relation(title, ROLLS_SHORT), "yes", title)
        self.assertEqual(story_relation(REDDIT, DUST), "yes")
        self.assertEqual(story_relation(SLUG, DUST), "yes")
        # The truncated Reddit slug has no Rolls-Royce, so it does not match the short headline.
        self.assertNotEqual(story_relation(SLUG, ROLLS_SHORT), "yes")

    def test_patch_and_fissure_sources(self):
        self.assertEqual(story_relation(PATCH_A, PATCH_B), "yes")
        self.assertEqual(story_relation(FISSURE_A, FISSURE_B), "yes")
        self.assertEqual(story_relation(FISSURE_EN, FISSURE_A), "yes")
        self.assertEqual(story_relation(FISSURE_EN, FISSURE_B), "yes")
        # A later note about one smoke is not the "patch is out" headline.
        self.assertNotEqual(
            story_relation("Патч CS2 починил смоки на Mirage", PATCH_A), "yes")

    def test_different_subjects_are_not_the_same_story(self):
        self.assertNotEqual(
            story_relation("NAVI выиграли IEM Beijing", "Spirit выиграли IEM Beijing"), "yes")
        self.assertNotEqual(
            story_relation("NAVI выиграли IEM Beijing", "NAVI выиграли BLAST"), "yes")
        self.assertNotEqual(
            story_relation("zonic о главных проблемах Falcons", "Falcons попрощались с академией"), "yes")
        self.assertNotEqual(
            story_relation("donk получил больше 1000 репортов на FACEIT", "Автовыбор серверов на FACEIT"), "yes")
        self.assertNotEqual(
            story_relation("NAVI 2:1 Falcons", "Falcons попрощались с академией"), "yes")
        # Same names, opposite facts: not a hard block. A tie-break may still look.
        self.assertEqual(
            story_relation("zonic остаётся в Falcons", "zonic ушёл из Falcons"), "maybe")

    def test_tie_break_only_on_maybe(self):
        left, right = "zonic остаётся в Falcons", "zonic ушёл из Falcons"
        self.assertIsNone(best_match(left, [right], tie_break=None))
        self.assertEqual(best_match(left, [right], tie_break=lambda a, b: False), None)
        self.assertEqual(best_match(left, [right], tie_break=lambda a, b: True), right)
        self.assertEqual(best_match(ROLLS_LONG, [ROLLS_SHORT], tie_break=_no_tie), ROLLS_SHORT)


class GeminiTieTest(unittest.TestCase):
    def tearDown(self):
        story_dedup._tie_cache.clear()

    def test_parse_yes_no(self):
        self.assertTrue(parse_yes_no("Да"))
        self.assertTrue(parse_yes_no("Yes, same story"))
        self.assertFalse(parse_yes_no("Нет, это другое"))
        self.assertFalse(parse_yes_no("no"))
        self.assertIsNone(parse_yes_no(""))
        self.assertIsNone(parse_yes_no("возможно"))

    def test_no_key_skips_the_call(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_API_KEY", None)
            with patch("requests.post") as post:
                self.assertIsNone(gemini_same_story("a", "b"))
                post.assert_not_called()

    def test_yes_no_uses_header_not_url(self):
        key = "AIza-test-key-not-real-0123456789"

        class Resp:
            def __init__(self, text, status=200):
                self.status_code = status
                self._text = text

            def json(self):
                return {"candidates": [{"content": {"parts": [
                    {"thought": True, "text": "reasoning да"},
                    {"text": self._text},
                ]}}]}

        def post(url, headers=None, json=None, timeout=None):
            post.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
            word = "нет" if len(post.calls) > 1 else "да"
            return Resp(word)

        post.calls = []
        with patch.dict(os.environ, {"GEMINI_API_KEY": key, "GEMINI_DEDUP_MODEL": "gemini-3.1-flash-lite"}):
            with patch("requests.post", post):
                story_dedup._tie_cache.clear()
                self.assertTrue(gemini_same_story("заголовок один", "другой заголовок"))
                story_dedup._tie_cache.clear()
                self.assertFalse(gemini_same_story("заголовок один", "совсем другой"))
        self.assertEqual(len(post.calls), 2)
        for call in post.calls:
            self.assertNotIn(key, call["url"])
            self.assertIn("gemini-3.1-flash-lite:generateContent", call["url"])
            self.assertEqual(call["headers"]["x-goog-api-key"], key)
            self.assertEqual(call["timeout"], 15)
            self.assertEqual(call["json"]["generationConfig"]["maxOutputTokens"], 16)
            self.assertNotIn("systemInstruction", call["json"])


class DigestWindowTest(unittest.TestCase):
    def test_news_topics_inside_48h_only(self):
        now = datetime.datetime(2026, 11, 5, 14, 3, tzinfo=MSK)
        recent = now - datetime.timedelta(hours=15)
        stale = now - datetime.timedelta(hours=50)
        with tempfile.TemporaryDirectory() as directory:
            self._write(directory, recent, "news", "FISSURE выплатила половину долгов в CS2", 327)
            # A result in the same file is not a news headline.
            path = os.path.join(directory, recent.strftime("%Y-%m-%d") + ".md")
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n## 10:01 · result · t.me/de_csnews/309\n- Тема: NAVI 2:0 B8\n- Факты: —\n- Источники: —\n")
            self._write(directory, stale, "news", "Старая новость про NAVI", 200)
            morning = datetime.datetime(2026, 11, 5, 11, 2, tzinfo=MSK)
            self._write(directory, morning, "news", ROLLS_SHORT, 331)
            got = digest_topics(now=now, directory=directory)
        self.assertEqual(got, [ROLLS_SHORT, FISSURE_A])
        self.assertEqual(NEWS_WINDOW_HOURS, 48)

    def _write(self, directory, moment, kind, topic, mid):
        path = os.path.join(directory, moment.strftime("%Y-%m-%d") + ".md")
        block = (
            f"# {moment:%Y-%m-%d}\n\n"
            f"## {moment:%H:%M} · {kind} · t.me/de_csnews/{mid}\n"
            f"- Тема: {topic}\n"
            f"- Факты: —\n"
            f"- Источники: https://example.com/{mid}\n"
        )
        mode = "a" if os.path.exists(path) else "w"
        with open(path, mode, encoding="utf-8") as f:
            if mode == "a":
                f.write("\n")
            f.write(block)


class FindDupTest(unittest.TestCase):
    def _log(self, hours_ago, first, words=None, scores=None, chat=CHANNEL_ID, mid=331):
        return {
            "ts": 1_700_000_000 - hours_ago * 3600,
            "chat": str(chat),
            "message_id": mid,
            "first": first,
            "words": words or [],
            "scores": scores or [],
        }

    def test_rolls_pair_blocks_after_six_hours(self):
        now = 1_700_000_000
        log = [self._log(6, ROLLS_SHORT, mid=331)]
        found = post.find_dup(
            _html(ROLLS_LONG), CHANNEL_ID, news=True, now=now, headlines=[], log=log, tie_break=None)
        self.assertIsNotNone(found)
        self.assertEqual(found["message_id"], 331)
        self.assertEqual(found["first"], ROLLS_SHORT)

    def test_same_pair_is_outside_a_three_hour_score_window(self):
        now = 1_700_000_000
        log = [self._log(6, ROLLS_SHORT, mid=331)]
        # Without the news window this is not a duplicate: no shared score, different first line.
        self.assertIsNone(post.find_dup(
            _html(ROLLS_LONG), CHANNEL_ID, news=False, now=now, log=log, tie_break=None))

    def test_rolls_pair_expires_after_48_hours(self):
        now = 1_700_000_000
        log = [self._log(50, ROLLS_SHORT, mid=331)]
        self.assertIsNone(post.find_dup(
            _html(ROLLS_LONG), CHANNEL_ID, news=True, now=now, headlines=[], log=log, tie_break=None))

    def test_patch_and_fissure_block_from_headlines(self):
        now = 1_700_000_000
        patch = post.find_dup(
            _html(PATCH_B), CHANNEL_ID, news=True, now=now, headlines=[PATCH_A], log=[], tie_break=None)
        fiss = post.find_dup(
            _html(FISSURE_B), CHANNEL_ID, news=True, now=now, headlines=[FISSURE_A], log=[], tie_break=None)
        self.assertEqual(patch["first"], PATCH_A)
        self.assertEqual(fiss["first"], FISSURE_A)

    def test_unrelated_headline_is_not_a_dup(self):
        now = 1_700_000_000
        log = [self._log(6, ROLLS_SHORT)]
        self.assertIsNone(post.find_dup(
            _html("Magnojez остается в BetBoom Team"), CHANNEL_ID, news=True,
            now=now, headlines=[], log=log, tie_break=None))

    def test_live_score_window_stays_three_hours(self):
        now = 1_700_000_000
        fresh = self._log(2, "Falcons 16:13 Ancient", words=["ancient", "falcons"], scores=["16:13"], mid=128)
        stale = self._log(6, "Falcons 16:13 Ancient", words=["ancient", "falcons"], scores=["16:13"], mid=123)
        body = "<b>Ancient 16:13 Falcons</b>\n\nСерия 1:0."
        self.assertEqual(
            post.find_dup(body, CHANNEL_ID, news=False, now=now, log=[fresh])["message_id"], 128)
        self.assertIsNone(post.find_dup(body, CHANNEL_ID, news=False, now=now, log=[stale]))
        exact = self._log(2, "Ancient 16:13 Falcons", mid=126)
        late = self._log(4, "Ancient 16:13 Falcons", mid=126)
        self.assertEqual(post.find_dup(body, CHANNEL_ID, news=False, now=now, log=[exact])["message_id"], 126)
        self.assertIsNone(post.find_dup(body, CHANNEL_ID, news=False, now=now, log=[late]))

    def test_other_chat_does_not_count(self):
        now = 1_700_000_000
        log = [self._log(1, ROLLS_LONG, chat="-100999")]
        self.assertIsNone(post.find_dup(
            _html(ROLLS_LONG), CHANNEL_ID, news=True, now=now, headlines=[], log=log, tie_break=None))


class ClassifyTest(unittest.TestCase):
    def test_batch_keeps_dust2_and_drops_reddit(self):
        dust = _item("dust:79654", "Dust2.us", DUST, "https://www.dust2.us/news/79654/rolls")
        reddit = _item("reddit:t3_1xoigx6", "Reddit", REDDIT, "https://www.reddit.com/r/GlobalOffensive/comments/1xoigx6/x")
        kept, blocked = news_auto.classify(
            [reddit, dust], published=[], state={"seen": {}}, tie_break=_no_tie, now=NOW)
        self.assertEqual([it["id"] for it in kept], ["dust:79654"])
        self.assertEqual([it["id"] for it, _why in blocked], ["reddit:t3_1xoigx6"])

    def test_published_russian_headline_blocks_both_sources(self):
        dust = _item("dust:79654", "Dust2.us", DUST, "https://www.dust2.us/news/79654/rolls")
        reddit = _item("reddit:t3_1xoigx6", "Reddit", REDDIT, "https://www.reddit.com/r/GlobalOffensive/comments/1xoigx6/x")
        kept, blocked = news_auto.classify(
            [dust, reddit], published=[ROLLS_SHORT], state={"seen": {}}, tie_break=_no_tie, now=NOW)
        self.assertEqual(kept, [])
        self.assertEqual({it["id"] for it, _why in blocked}, {"dust:79654", "reddit:t3_1xoigx6"})

    def test_recent_posted_english_title_blocks_reddit(self):
        reddit = _item("reddit:t3_1xoigx6", "Reddit", REDDIT, "https://www.reddit.com/r/GlobalOffensive/comments/1xoigx6/x")
        state = {"seen": {"dust:79654": {
            "status": "posted",
            "title": DUST,
            "url": "https://www.dust2.us/news/79654/rolls",
            "marked_at": "2026-11-05T11:02:00+03:00",
        }}}
        kept, blocked = news_auto.classify(
            [reddit], published=[], state=state, tie_break=_no_tie, now=NOW)
        self.assertEqual(kept, [])
        self.assertEqual(blocked[0][0]["id"], "reddit:t3_1xoigx6")

    def test_old_posted_title_does_not_block(self):
        reddit = _item("reddit:t3_1xoigx6", "Reddit", REDDIT, "https://www.reddit.com/r/GlobalOffensive/comments/1xoigx6/x")
        state = {"seen": {"dust:1": {
            "status": "posted", "title": DUST, "url": "https://www.dust2.us/news/1",
            "marked_at": "2026-10-01T11:02:00+03:00",
        }}}
        kept, blocked = news_auto.classify(
            [reddit], published=[], state=state, tie_break=_no_tie, now=NOW)
        self.assertEqual([it["id"] for it in kept], ["reddit:t3_1xoigx6"])
        self.assertEqual(blocked, [])

    def test_patch_and_fissure_candidates_are_blocked(self):
        patch = _item("tg:cs3news:19528", "Telegram / cs3news", PATCH_B, "https://t.me/cs3news/19528")
        fiss = _item("tg:acenewscs:17010", "Telegram / acenewscs", FISSURE_B, "https://t.me/acenewscs/17010")
        other = _item("hltv:45918", "HLTV", "BLAST Rivals Season 2 talent announced", "https://www.hltv.org/news/45918/x")
        kept, blocked = news_auto.classify(
            [patch, fiss, other],
            published=[PATCH_A, FISSURE_A],
            state={"seen": {}},
            tie_break=_no_tie,
            now=NOW,
        )
        self.assertEqual([it["id"] for it in kept], ["hltv:45918"])
        self.assertEqual({it["id"] for it, _why in blocked}, {"tg:cs3news:19528", "tg:acenewscs:17010"})


class PostDryRunTest(unittest.TestCase):
    def _run(self, headline, log, digest_blocks, extra_args=()):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log_path = os.path.join(tmp.name, "sent_log.json")
        digest = os.path.join(tmp.name, "daily")
        os.makedirs(digest)
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(log, f)
        for day, text in digest_blocks:
            with open(os.path.join(digest, day + ".md"), "w", encoding="utf-8") as f:
                f.write(text)
        draft = os.path.join(tmp.name, "post.txt")
        with open(draft, "w", encoding="utf-8") as f:
            f.write(_html(headline) + "\n")
        env = os.environ.copy()
        env["SENT_LOG"] = log_path
        env["DIGEST_DIR"] = digest
        env.pop("GEMINI_API_KEY", None)
        env.pop("TELEGRAM_BOT_TOKEN", None)
        return subprocess.run(
            [sys.executable, "post.py", "reddit:t3_1xoigx6", "--text-file", draft,
             "--url", "https://www.reddit.com/r/GlobalOffensive/comments/1xoigx6/x",
             "--source", "Reddit", "--image", "", "--dry-run", *extra_args],
            cwd=ROOT, capture_output=True, text=True, env=env,
        )

    def test_dry_run_refuses_the_rolls_pair(self):
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        log = [{
            "ts": now - 6 * 3600,
            "chat": str(CHANNEL_ID),
            "message_id": 331,
            "first": ROLLS_SHORT,
            "words": ["rolls-royce"],
            "scores": [],
        }]
        proc = self._run(ROLLS_LONG, log, [])
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DUPLICATE:", proc.stderr)
        self.assertIn(ROLLS_SHORT, proc.stderr)
        self.assertNotIn("sendMessage", proc.stdout)

    def test_dry_run_allows_a_different_story(self):
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        log = [{
            "ts": now - 6 * 3600, "chat": str(CHANNEL_ID), "message_id": 331,
            "first": ROLLS_SHORT, "words": [], "scores": [],
        }]
        proc = self._run("Magnojez остается в BetBoom Team", log, [])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('"method": "sendMessage"', proc.stdout)

    def test_dry_run_reads_digest_for_fissure(self):
        moment = datetime.datetime.now(MSK) - datetime.timedelta(hours=15)
        day = moment.strftime("%Y-%m-%d")
        text = textwrap.dedent(f"""\
            # {day}

            ## {moment:%H:%M} · news · t.me/de_csnews/327
            - Тема: {FISSURE_A}
            - Факты: —
            - Источники: https://www.hltv.org/news/45916/fissure

            ## 10:01 · result · t.me/de_csnews/309
            - Тема: NAVI 2:0 B8
            - Факты: —
            - Источники: —
            """)
        proc = self._run(FISSURE_B, [], [(day, text)])
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DUPLICATE:", proc.stderr)
        self.assertIn(FISSURE_A, proc.stderr)

    def test_force_dup_still_prints_the_payload(self):
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        log = [{
            "ts": now - 6 * 3600, "chat": str(CHANNEL_ID), "message_id": 331,
            "first": ROLLS_SHORT, "words": [], "scores": [],
        }]
        proc = self._run(ROLLS_LONG, log, [], extra_args=("--force-dup",))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('"method": "sendMessage"', proc.stdout)
        self.assertNotIn("DUPLICATE:", proc.stderr)


if __name__ == "__main__":
    unittest.main()
