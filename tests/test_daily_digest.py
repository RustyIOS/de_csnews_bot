"""Daily channel digest: format, idempotency, Moscow midnight, dry-run."""
import datetime
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import CHANNEL_ID, MSK
import daily_digest
from daily_digest import record_sent

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UTC = datetime.timezone.utc


RESULT_TEXT = textwrap.dedent("""\
    🏁 <b>NAVI 2:1 Spirit</b>

    🏆 BLAST Premier
    Mirage 13:9, Inferno 8:13, Nuke 13:11

    #CS2 #BLAST
    """).strip()

RESULT_CARD = {
    "event": "BLAST Premier",
    "stage": "Группа A",
    "format": "bo3",
    "team1": "NAVI",
    "team2": "Spirit",
    "score": "2:1",
    "maps": [
        {"map": "Mirage", "score": "13:9"},
        {"map": "Inferno", "score": "8:13"},
        {"map": "Nuke", "score": "13:11"},
    ],
    "player": "donk",
    "url": "https://bo3.gg/matches/navi-spirit",
    "liquipedia_url": "https://liquipedia.net/counterstrike/BLAST/2026",
    "team1_logo": "https://cdn.example/navi.png",
}

ANNOUNCE_TEXT = textwrap.dedent("""\
    📅 <b>Матчи дня, 1 ноября</b>

    🏆 BLAST Premier
    18:00 NAVI vs Spirit, bo3, Группа A

    🏆 IEM Cologne
    20:00 FaZe vs Liquid, bo1, Полуфинал

    #CS2 #BLAST #IEM
    """).strip()

NEWS_TEXT = textwrap.dedent("""\
    <b>Vitality подписала игрока</b>

    Текст. <a href="https://liquipedia.net/counterstrike/Vitality">профиль</a>

    #CS2 #Vitality
    """).strip()


def _news(message_id, when, directory, chat=CHANNEL_ID):
    return record_sent(
        message_id=message_id,
        chat=chat,
        item_id="hltv:1",
        body="<b>Короткая новость</b>\n\nДва предложения тут.\n\n#CS2 #Новости",
        item={"url": "https://www.hltv.org/news/1", "source": "HLTV", "title": "English title"},
        when=when,
        directory=directory,
    )


class DailyDigestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = os.path.join(self.tmp.name, "daily")

    def test_format_result_announce_and_news(self):
        import json
        drafts = os.path.join(self.tmp.name, "drafts")
        os.makedirs(drafts)
        text_path = os.path.join(drafts, "live-end-bo3-1.txt")
        with open(text_path, "w", encoding="utf-8") as f:
            f.write(RESULT_TEXT + "\n")
        with open(os.path.join(drafts, "live-end-bo3-1.json"), "w", encoding="utf-8") as f:
            json.dump(RESULT_CARD, f)

        when = datetime.datetime(2026, 11, 1, 18, 5, tzinfo=MSK)
        path = record_sent(
            message_id=321,
            chat=CHANNEL_ID,
            item_id="live-end-bo3-1",
            body=RESULT_TEXT,
            item={"url": "https://bo3.gg/matches/navi-spirit", "source": "bo3.gg", "title": "NAVI vs Spirit"},
            text_file=text_path,
            when=when,
            directory=self.directory,
        )
        self.assertEqual(path, os.path.join(self.directory, "2026-11-01.md"))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), textwrap.dedent("""\
                # 2026-11-01

                ## 18:05 · result · t.me/de_csnews/321
                - Тема: NAVI 2:1 Spirit
                - Факты: турнир: BLAST Premier; стадия: Группа A; команды: NAVI — Spirit; формат: bo3; счёт: 2:1; карты: Mirage 13:9, Inferno 8:13, Nuke 13:11; игроки: donk
                - Источники: https://bo3.gg/matches/navi-spirit, https://liquipedia.net/counterstrike/BLAST/2026
                """))

        ann = record_sent(
            message_id=322,
            chat="@de_csnews",
            item_id="ann-2026-11-01",
            body=ANNOUNCE_TEXT,
            item={"url": "https://bo3.gg/matches/999001", "source": "bo3.gg"},
            when=datetime.datetime(2026, 11, 1, 10, 4, tzinfo=MSK),
            directory=self.directory,
        )
        with open(ann, encoding="utf-8") as f:
            announce_file = f.read()
        self.assertIn("\n## 10:04 · announce · t.me/de_csnews/322\n", announce_file)
        self.assertIn("- Тема: Матчи дня, 1 ноября\n", announce_file)
        self.assertIn(
            "- Факты: турнир: BLAST Premier; стадия: Группа A; команды: NAVI — Spirit; формат: bo3"
            " | турнир: IEM Cologne; стадия: Полуфинал; команды: FaZe — Liquid; формат: bo1\n",
            announce_file,
        )
        self.assertIn("- Источники: https://bo3.gg/matches/999001\n", announce_file)

        news = record_sent(
            message_id=323,
            chat=CHANNEL_ID,
            msg_chat={"id": int(CHANNEL_ID), "username": "de_csnews"},
            item_id="x:99",
            body=NEWS_TEXT,
            item={
                "url": "https://x.com/HLTVorg/status/1",
                "source": "X / @HLTVorg",
                "title": "English title should not replace the Russian headline",
                "image": "https://cdn.example/photo.jpg",
                "x": {"links": ["https://www.hltv.org/news/999", "https://x.com/HLTVorg/status/1"]},
            },
            when=datetime.datetime(2026, 11, 1, 13, 51, tzinfo=MSK),
            directory=self.directory,
        )
        with open(news, encoding="utf-8") as f:
            news_file = f.read()
        self.assertIn("\n## 13:51 · news · t.me/de_csnews/323\n", news_file)
        self.assertIn("- Тема: Vitality подписала игрока\n", news_file)
        self.assertIn("- Факты: —\n", news_file)
        self.assertIn(
            "- Источники: https://x.com/HLTVorg/status/1, https://www.hltv.org/news/999, "
            "https://liquipedia.net/counterstrike/Vitality\n",
            news_file,
        )
        self.assertNotIn("photo.jpg", news_file)
        self.assertNotIn("English title", news_file)

    def test_same_message_id_is_written_once(self):
        when = datetime.datetime(2026, 11, 1, 12, 0, tzinfo=MSK)
        first = _news(50, when, self.directory)
        with open(first, encoding="utf-8") as f:
            original = f.read()
        again = record_sent(
            message_id="50",
            chat=CHANNEL_ID,
            item_id="hltv:other",
            body="<b>Другой заголовок</b>\n\nДругой текст.\n\n#CS2 #Новости",
            item={"url": "https://www.hltv.org/news/2", "source": "HLTV"},
            when=datetime.datetime(2026, 11, 2, 9, 0, tzinfo=MSK),
            directory=self.directory,
        )
        self.assertEqual(again, first)
        with open(first, encoding="utf-8") as f:
            self.assertEqual(f.read(), original)
        self.assertEqual(original.count("## "), 1)
        self.assertFalse(os.path.exists(os.path.join(self.directory, "2026-11-02.md")))

        second = _news(51, datetime.datetime(2026, 11, 1, 12, 30, tzinfo=MSK), self.directory)
        self.assertEqual(second, first)
        with open(first, encoding="utf-8") as f:
            combined = f.read()
        self.assertEqual(combined.count("## "), 2)
        self.assertIn("t.me/de_csnews/50", combined)
        self.assertIn("t.me/de_csnews/51", combined)
        self.assertEqual(combined.count("t.me/de_csnews/50"), 1)

    def test_msk_date_rolls_over_at_midnight(self):
        late = _news(10, datetime.datetime(2026, 11, 1, 23, 59, tzinfo=MSK), self.directory)
        early = _news(11, datetime.datetime(2026, 11, 2, 0, 1, tzinfo=MSK), self.directory)
        # 21:00 UTC is 00:00 the next day in Moscow (UTC+3, no DST).
        from_utc = _news(12, datetime.datetime(2026, 11, 1, 21, 0, tzinfo=UTC), self.directory)
        self.assertEqual(os.path.basename(late), "2026-11-01.md")
        self.assertEqual(os.path.basename(early), "2026-11-02.md")
        self.assertEqual(from_utc, early)
        with open(late, encoding="utf-8") as f:
            self.assertIn("## 23:59 · news · t.me/de_csnews/10\n", f.read())
        with open(early, encoding="utf-8") as f:
            rolled = f.read()
        self.assertTrue(rolled.startswith("# 2026-11-02\n"))
        self.assertIn("## 00:01 · news · t.me/de_csnews/11\n", rolled)
        self.assertIn("## 00:00 · news · t.me/de_csnews/12\n", rolled)
        self.assertNotIn("2026-11-01", rolled)

    def test_prune_keeps_thirty_days(self):
        os.makedirs(self.directory)
        keep = os.path.join(self.directory, "2026-10-02.md")
        drop = os.path.join(self.directory, "2026-10-01.md")
        with open(keep, "w", encoding="utf-8") as f:
            f.write("# 2026-10-02\n")
        with open(drop, "w", encoding="utf-8") as f:
            f.write("# 2026-10-01\n")
        _news(7, datetime.datetime(2026, 11, 1, 12, 0, tzinfo=MSK), self.directory)
        self.assertTrue(os.path.isfile(keep))
        self.assertFalse(os.path.exists(drop))
        self.assertTrue(os.path.isfile(os.path.join(self.directory, "2026-11-01.md")))
        removed = daily_digest.prune(self.directory, today=datetime.date(2026, 11, 1))
        self.assertEqual(removed, [])

    def test_dry_run_writes_nothing(self):
        os.makedirs(self.directory)
        existing = os.path.join(self.directory, "2026-11-01.md")
        with open(existing, "w", encoding="utf-8") as f:
            f.write("# keep\n")
        with open(existing, encoding="utf-8") as f:
            before = f.read()
        self.assertIsNone(record_sent(
            message_id=1,
            chat=CHANNEL_ID,
            item_id="ann-2026-11-01",
            body=ANNOUNCE_TEXT,
            item={"url": "https://bo3.gg/matches", "source": "bo3.gg"},
            when=datetime.datetime(2026, 11, 1, 10, 4, tzinfo=MSK),
            dry_run=True,
            directory=self.directory,
        ))
        with patch.dict(os.environ, {"GHA_DRY_RUN": "true"}, clear=False):
            self.assertIsNone(_news(2, datetime.datetime(2026, 11, 1, 10, 5, tzinfo=MSK), self.directory))
        with patch.dict(os.environ, {"DRY_RUN": "1", "GHA_DRY_RUN": ""}, clear=False):
            self.assertIsNone(_news(3, datetime.datetime(2026, 11, 1, 10, 6, tzinfo=MSK), self.directory))
        with open(existing, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)
        names = os.listdir(self.directory)
        self.assertEqual(names, ["2026-11-01.md"])

        self.assertIsNone(record_sent(
            message_id=4,
            chat="-100999",
            item_id="hltv:9",
            body=NEWS_TEXT,
            item={"url": "https://www.hltv.org/news/9", "source": "HLTV"},
            when=datetime.datetime(2026, 11, 1, 10, 7, tzinfo=MSK),
            directory=self.directory,
        ))
        with open(existing, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)

    def test_post_py_dry_run_does_not_touch_digest(self):
        digest = os.path.join(self.tmp.name, "from-post")
        text = os.path.join(self.tmp.name, "post.txt")
        with open(text, "w", encoding="utf-8") as f:
            f.write("<b>Заголовок</b>\n\nТекст новости для проверки.\n\n#CS2 #Новости\n")
        env = os.environ.copy()
        env["DIGEST_DIR"] = digest
        env.pop("TELEGRAM_BOT_TOKEN", None)
        env.pop("GHA_DRY_RUN", None)
        env.pop("DRY_RUN", None)
        proc = subprocess.run(
            [sys.executable, "post.py", "x:1", "--text-file", text,
             "--url", "https://www.hltv.org/news/1", "--source", "HLTV",
             "--image", "", "--dry-run"],
            cwd=ROOT, capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('"method": "sendMessage"', proc.stdout)
        self.assertFalse(os.path.exists(digest))
        self.assertFalse(os.path.isdir(os.path.join(ROOT, "daily")))

    def test_live_and_day_recap_types(self):
        recap = """🏁 <b>Итоги дня, 1 ноября</b>

🏆 BLAST Premier
NAVI 2:1 Spirit (Mirage 13:9, Inferno 8:13)

#CS2 #BLAST"""
        start = """🟢 <b>Матч начался: NAVI — Spirit</b>

🏆 BLAST Premier
bo3, Группа A

#CS2 #BLAST"""
        when = datetime.datetime(2026, 11, 1, 23, 34, tzinfo=MSK)
        path = record_sent(
            message_id=8,
            chat=CHANNEL_ID,
            item_id="res-2026-11-01",
            body=recap,
            item={"url": "https://bo3.gg/matches/1", "source": "bo3.gg"},
            when=when,
            directory=self.directory,
        )
        record_sent(
            message_id=9,
            chat=CHANNEL_ID,
            item_id="live-start-bo3-1",
            body=start,
            item={
                "url": "https://bo3.gg/matches/2",
                "source": "bo3.gg",
                "liquipedia_url": "https://liquipedia.net/counterstrike/BLAST",
            },
            when=datetime.datetime(2026, 11, 1, 18, 0, tzinfo=MSK),
            directory=self.directory,
        )
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("## 23:34 · day recap · t.me/de_csnews/8\n", text)
        self.assertIn(
            "- Факты: турнир: BLAST Premier; команды: NAVI — Spirit; счёт: 2:1; карты: Mirage 13:9, Inferno 8:13\n",
            text,
        )
        self.assertIn("## 18:00 · live · t.me/de_csnews/9\n", text)
        self.assertIn(
            "- Факты: турнир: BLAST Premier; стадия: Группа A; команды: NAVI — Spirit; формат: bo3\n",
            text,
        )
        self.assertIn(
            "- Источники: https://bo3.gg/matches/2, https://liquipedia.net/counterstrike/BLAST\n",
            text,
        )
