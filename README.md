# CS2 news -> @de_csnews (bot @de_csnews_bot)

## GitHub Actions

Анонс матчей, «Итоги дня» и короткий лайв по топ-турнирам запускает `.github/workflows/csnews.yml`. Свой компьютер для этого не нужен: cron у GitHub в UTC, Москва круглый год UTC+3.

| Задача | Когда | Если слот пропущен |
| --- | --- | --- |
| Анонс (карточка + список матчей дня) | 10:04 МСК | догон до 12:00, один пост в день |
| Новости, до 3 постов | 10:51, 13:51, 16:51, 19:51, 22:51 МСК | догон 20 минут, слот один раз |
| Итоги дня, включая стримерскую лигу | 23:34 МСК | догон до 23:59, один пост в день |
| Лайв, только топ (`live_tiers: top`) | каждые 10 мин, 10:00–23:59 МСК | устаревшее схлопывается, не больше 2 постов за запуск |

Лайв пишет старт матча и финальный счёт (карточка `result`). Половина, старт карты и результат карты не пишутся: на десятиминутном шаге эти счета уже бывали неверными, и опоздавший запуск не должен высыпать цепочку. Стримерская лига и Stake Pulse в лайв не входят — они остаются в анонсе и в итогах. Опросы выключены.

Пока переменная `POSTING_ENABLED` не равна `true`, ни cron, ни внешний вызов ничего не отправляют.

### Секреты

1. Репозиторий → Settings → Secrets and variables → Actions → вкладка Secrets → New repository secret.
2. Name: `TELEGRAM_BOT_TOKEN`. Value: токен бота от BotFather. В логи и в git он не попадает.
3. Name: `GEMINI_API_KEY`. Value: ключ из [Google AI Studio](https://aistudio.google.com/apikey). Основной черновик новостей. В логи и в git он не попадает. Если секрета нет или он пустой, новости черновит только OpenRouter, как раньше.
4. Name: `OPENROUTER_API_KEY`. Value: ключ с [openrouter.ai/keys](https://openrouter.ai/keys). Запасной черновик новостей, если Gemini не задан или не вернул целый JSON. В логи и в git он не попадает.
5. Чат уже задан в `common.py`: `-1003635521376` (@de_csnews).

Сухой прогон анонса, итогов и лайва токен не требует. Сухой прогон новостей вызывает Gemini, когда задан `GEMINI_API_KEY`, иначе OpenRouter. Без обоих ключей черновиков не сделает. Боевой запуск без секрета бота остановится в `post.py` до отправки.

### Включить публикацию

Это переменная репозитория, не секрет. Settings → Secrets and variables → Actions → Variables → New repository variable:

- Name: `POSTING_ENABLED`
- Value: `true`

Пока переменной нет, она пустая или равна чему-то кроме `true` / `1` / `yes`, cron не стартует. Чтобы снова выключить публикации, удалите переменную или поставьте `false`.

Перед `true` остановите на боксе `sched/` (анонс, итоги и коллектор). У бокса и у Actions разные копии состояния, поэтому два планировщика пришлют два поста. Состояние прогонов Actions лежит в ветке `cursor/csnews-state-a760` (`sent_log.json`, `state.json`, `auto_state.json`, `live_queue.json`, `collector_state.json`) и коммитится после отправки. Просмотренные новости — это `seen` в `state.json`, слоты новостей — `news_slots` в `auto_state.json`. Одновременно идёт только один прогон (concurrency `de-csnews`).

Остаются прежние предохранители: `check_post.py`, проверка картинки, отказ от дубля за 3 часа, пауза 75 с между постами, один анонс и одни итоги на дату, один проход новостей на слот.

### Внешний таймер (cron-job.org)

Собственный cron GitHub часто пропускает слоты. Каждые 10 минут внешний таймер вызывает тот же workflow. Запрос без `inputs` или с `"job": "auto"` ведёт себя как плановый запуск: по московскому времени решает, что пора (анонс 10:04–12:00, новости в окне :51 и ещё 20 минут, итоги 23:34–23:59, иначе лайв 10:00–23:59), и при `POSTING_ENABLED=true` отправляет по-настоящему. В одном запуске сначала анонс, потом итоги, потом новости, если открыт их слот, иначе лайв. Повторные вызовы безопасны: один анонс и одни итоги в сутки, новости один раз на слот, лайв молчит, если нового нет, параллельный запуск ждёт в группе `de-csnews`.

```
POST https://api.github.com/repos/RustyIOS/de_csnews_bot/actions/workflows/csnews.yml/dispatches
```

Заголовки:

```
Accept: application/vnd.github+json
Authorization: Bearer <FINE_GRAINED_PAT>
X-GitHub-Api-Version: 2022-11-28
Content-Type: application/json
```

Тело (явно то же, что и пустой `inputs`):

```json
{"ref":"main","inputs":{"job":"auto"}}
```

Без inputs тоже достаточно — сработают значения по умолчанию `job=auto` и `dry_run=false`:

```json
{"ref":"main"}
```

Ответ API — `204` без тела. Это нормально, не ошибка.

Токен: fine-grained PAT только на этот репозиторий (`RustyIOS/de_csnews_bot`). Права: **Actions — Read and write**. Других прав не нужно (Contents, Administration и остальные — No access).

Сухой прогон тем же вызовом, в канал ничего не уходит:

```json
{"ref":"main","inputs":{"job":"auto","dry_run":"true"}}
```

`dry_run` в API — строка `"true"` или `"false"`. В логе будет текст, карточка — в артефакте `csnews-dry-run`. Кнопка Run workflow в интерфейсе GitHub с настройками по умолчанию тоже отправляет, если что-то уже пора и `POSTING_ENABLED=true`; для проверки включите dry_run.

В таймер кладите `job=auto` или тело без `inputs`. Значения `announce`, `results`, `live` и `news` — ручной прогон: они не смотрят на окно.

Итоги 17 октября уже отмечены в `auto_state.json` (пост с бокса, https://t.me/de_csnews/147), повторный догон их не отправит.

### Новости (Gemini, запасной OpenRouter)

Отдельный проход `news_auto.py` заменяет рутину Grok для обычных новостей. Лайв по-прежнему только топ, «Итоги дня» по-прежнему включают стримерскую лигу. Слоты: 10:51, 13:51, 16:51, 19:51, 22:51 МСК. Если запуск опоздал меньше чем на 20 минут, слот ещё берётся. Позже он пропускается до следующего.

Каждый слот:

1. `fetch.py` — HLTV, Dust2, PC Gamer, Dexerto, Steam, Reddit.
2. X только через публичный RSS аккаунтов `core` из `x_accounts.json`. Если зеркало не ответило, X пропускается. Ключ X и инструменты Grok не нужны.
3. Публичные превью `t.me/s/` каналов из `tg_channels.json` с `"news": true`.
4. Уже виденное отсекается по `state.json` (`seen`) и по похожему заголовку. Результаты матчей, мемы и гайды не идут в черновик: их закрывают лайв и итоги.
5. Один короткий запрос, не больше 3 постов. Сначала Gemini: `gemini-3.5-flash`, затем `gemini-3.1-flash-lite` (переопределение: переменная `GEMINI_MODELS` через запятую). Ответ запрашивается как JSON (`responseMimeType=application/json`). Таймаут около 60 с. При 429, 5xx или таймауте скрипт берёт следующую модель Gemini, без повтора той же. Ответ, который не является целым `{"posts":[...]}`, один раз переспрашивается строже на той же модели, потом берётся следующая. Если ключа Gemini нет или ни одна модель Gemini не вернула такой JSON, тот же промпт уходит в OpenRouter: `dots-studio/dots-3-note-preview:free`, затем `openrouter/free` (переопределение: `OPENROUTER_MODELS`). В логе видно, какой провайдер и какая модель собрали черновик. Системный промпт: живой русский CS-канала, жирный заголовок, 2–4 предложения только из источника, слух помечен, 2–4 хэштега и `#de_csnews`, без строки «Источник» (её добавляет `post.py` обычным текстом).
6. `check_post.py`, затем `post.py --dry-run`, и только потом отправка. Дубль за 3 часа и пауза 75 с — те же, что у остальных постов.

Бесплатный OpenRouter без кредитов — около 50 запросов в сутки и легко отвечает 429. Он остаётся запасом: при 429 скрипт ждёт 5 секунд и повторяет один раз, потом берёт следующую модель. Пять слотов в день в этот лимит укладываются, а при живом Gemini OpenRouter чаще всего не вызывается. Пока ни Gemini, ни OpenRouter не вернули целый JSON (или ключей нет), слот не отмечается и догон внутри 20 минут пробует ещё раз. То же самое, если разбор удался, но не ушло ни одного поста: каждый выбранный черновик не прошёл проверку формата или каждая отправка сорвалась. Такой черновик не помечается просмотренным, чтобы следующий заход мог взять его снова. Пустой ответ `{"posts":[]}`, отсутствие кандидатов и хотя бы одна успешная отправка слот закрывают.

Локально: `python3 news_auto.py --dry-run` или `python3 scripts/gha_run.py news --dry-run`. Часы: `python3 scripts/gha_run.py tick`. Итоги за дату: `python3 results_auto.py --dry-run --date 2026-10-17`.

Sources: HLTV RSS, Dust2.us RSS, PC Gamer CS2 tag RSS, Dexerto (all-news RSS, filtered to CS), Steam official CS2 announcements (app 730),
Reddit r/GlobalOffensive (in fetch.py). Actions берёт X из публичного RSS и не вызывает X-инструменты Grok (раздел «Новости» выше). Пока новости идут из Actions, почасовую рутину на боксе не запускайте: у неё своя копия `state.json`, и канал получит два поста.
Hourly routine (box only):
  python3 fetch.py > new.json          # or --brief; never marks seen; also writes items.json
  # write Russian HTML text to post.txt (bold headline, 2-4 sentences)
  python3 post.py ID --text-file post.txt --dry-run
  python3 news_queue.py --add ID --text-file post.txt [--image X]   # SEND NEWS THIS WAY (not post.py directly):
      # re-runs check_post.py + post.py --dry-run, schedules at last news + random 10-15 min (first one goes out now if
      # no news in the last 10 min), freezes text/url/source/image, starts a detached sender (pid news_queue.json.pid);
      # live.py also runs `news_queue.py --send-due` each run as a backup. One draft file per item (copied on --add).
      # --list | --cancel ID | --retry ID (status 'sending' after a crash = check channel first) | log logs/news_queue.log
      # Only regular news. Live posts, announcements/results, polls, daily/weekly recaps -> post.py/poll.py directly.
  python3 post.py ID --text-file post.txt      # direct send (non-news posts only), marks posted
  python3 mark_seen.py ID ...                  # skip uninteresting/duplicate items
  python3 mark_seen.py --unmark ID | --list
Token: env TELEGRAM_BOT_TOKEN. Test target: post.py ... --chat <chat_id>.

Matches (source: bo3.gg public JSON API, fallback Liquipedia API; nothing is posted by these):
  python3 matches.py --today [--brief] [--unposted] [--all] [--date YYYY-MM-DD]   # today's notable matches, MSK
  python3 matches.py --results [--hours 24] [--brief] [--unposted]               # finished, score + maps
  python3 matches.py --mark announced KEY ... | --mark result KEY ...           # after posting (state: match_announcements / match_results)
  python3 matches.py --unmark announced|result KEY ...
  python3 matches.py --tournaments [--days 14] [--include-current] [--unposted] [--brief]   # upcoming big events
  python3 matches.py --mark tournament KEY ...                                              # after a tournament preview post
    bo3.gg tournaments API (tier S, or tier A whose name matches Major/IEM/BLAST/EPL/PGL...), detail call per event for
    city/country/LAN-online/known teams; enriched with Liquipedia "S-Tier_Tournaments" (teams count P#, location; page
    cached 6 h in lp_tournaments_cache.json because of Liquipedia's parse rate limit). LP-only S-tier events in the window
    are appended (source Liquipedia). Fields: key, name, start_date/end_date (MSK), days_until, prize (USD), teams_count
    (LP first, else bo3 teams_known count), location, event_type, logo, url, liquipedia_url, announced.
    Location/country names are English -> translate in post/card text (e.g. "Мальта").
  Match JSON now also has team1_logo / team2_logo / event_logo (bo3 image URLs) for card.py.
  Notable = tier S/A event, or B-tier with a top-20 team, or both teams top-30 (--max-rank). --source bo3|liquipedia to force.
  Announcement/result text posts: python3 post.py ann-YYYY-MM-DD --text-file t.txt --url <match url> --source bo3.gg --image '' --no-mark
POLLS ARE OFF (14.10.2026, user request): no routine posts polls or poll results. Do NOT run poll.py -q, do NOT
  render/post card.py poll_result, do NOT write "голосуй в опросе" in any post. Announce = text/card only; match start /
  result = card only. poll.py refuses to send and card.py refuses poll_result while common.POLLS_ENABLED=False.
  Already-open old polls: leave them (optionally poll.py --stop KEY to close, but post nothing about it).
  Re-enable only on user request: POLLS_ENABLED=True in common.py.
Polls (DISABLED, see above; anonymous, regular; state: polls):
  python3 poll.py -q "Кто выиграет?" -o Vitality -o MOUZ --key bo3:130754 [--dry-run] [--chat ID]
  python3 poll.py --stop bo3:130754 [--winner Vitality]     # close poll (stopPoll), store + print JSON with votes/percent
  python3 poll.py --results bo3:130754 [--winner Vitality]  # stored results; if still open: live peek via getUpdates
  python3 poll.py --list                                     #   (read-only, no offset -> never confirms/consumes updates)
  Results JSON: {key, question, closed, total_voters, options:[{text,votes,percent}], results, winner?, guessed_percent?}
  --winner adds guessed_percent (share who picked the actual winner) and is stored only on --stop.
  The JSON can be piped into the card:  python3 poll.py --stop KEY --winner X | python3 card.py poll_result --json - -o cards/out/poll.jpg
- post.py automatically appends #de_csnews to the hashtag line (or adds it if missing).

Reddit (fetch.py, source label "Reddit"; --no-reddit to skip):
  reddit.com JSON (www/old/api) is login-walled/403 from this box, so it reads the Atom feed
  https://www.reddit.com/r/GlobalOffensive/top/.rss?t=day&limit=50 (no score/flair in Atom; feed order = score).
  Keep: news (transfers/quotes/updates/drama, external article links) rank <= 25; discussion (selftext >= 300 chars)
  and pro highlight clips rank <= 10. Skip: match/post-match threads, help/tech-support/questions, guides/lineups,
  memes / media without context. Item extras: item["reddit"] = {kind, rank, score, flair, comments, author, link}.
  Optional: set REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET (free Reddit app, app-only OAuth) -> oauth.reddit.com JSON with
  real score >= 1000 (highlights >= 2000), stickied/flair (Help, Meme, Fluff, Match Thread...) filtering, flair+score in item.

X / Twitter (routine agent fetches; box scripts cannot call X). Accounts + IDs: x_accounts.json (group core | teams).
Budget: 30 reads/min, 1000/day (shared). Recipe, every hourly run AFTER fetch.py (fetch.py rewrites items.json):
  1. START = now-75min in UTC:  date -u -d '-75 min' +%FT%TZ
     If x_gap_start.txt exists (a previous run got X 429 / no data), use the EARLIER of it and now-75min as START,
     then delete the file after a successful read (x_ingest dedupes, so overlap is safe). On 429, write the START you
     couldn't cover into x_gap_start.txt (keep an older value if one is already there).
     (first run of the day, 9:51: START = 23:45 MSK yesterday, and follow next_token up to 3 pages per query)
  2. Two CallDynamicTool calls, namespace "x", tool "search_posts_all", write_to_file: true, arguments:
       query (core):  (from:CounterStrike OR from:HLTVorg OR from:ESLCS OR from:BLASTPremier OR from:pglesports OR from:Sheep_VALORANT OR from:Mauisnake OR from:Thorin) -is:retweet -is:reply
       query (teams): (from:TeamVitalityCS OR from:mousesports OR from:Team__Spirit OR from:natusvincere OR from:FaZeEsports OR from:G2CSGO OR from:FURIA OR from:FalconsEsport OR from:AuroraCS2_GG OR from:1mongolz OR from:parivisiongg OR from:AstralisCS OR from:TeamLiquidCS OR from:heroicgg OR from:3DMAXGaming OR from:MIBR OR from:GamerLegion OR from:NIPCS OR from:B8esportsGG OR from:virtuspro OR from:eternalfiregg) -is:retweet -is:reply
       plus: "start_time": START, "max_results": 25 (API max per call), "sort_order": "recency",
             "expansions": "author_id,attachments.media_keys", "media.fields": "type,url,preview_image_url",
             "post.fields": "created_at,author_id,referenced_tweets,entities,public_metrics,attachments", "user.fields": "username"
     If a result has meta.next_token (25 results), call again with "next_token": <it> (one extra page; 3 on the morning run).
     => 2-4 reads/hour, ~30-60/day.
  3. python3 x_ingest.py /workspace/agent-tools/<core>.txt /workspace/agent-tools/<teams>.txt --brief
     (accepts raw tool dumps, JSON lists of raw responses, or simplified [{id, username, text, created_at, url, image}];
      '-' = stdin; --dry-run = don't write; --min-likes N; --hours 24). Appends items "x:<id>", source "X / @user" to items.json.
  4. Pick/post as usual: python3 post.py x:<id> --text-file post.txt [--dry-run];  python3 mark_seen.py x:<id> ... to skip.
  Filters in x_ingest: last 24h, no replies/retweets (quotes kept), no promo/sponsor/betting/stream-plugs, no near-empty
  hype ("mood", "W day"), no other-game posts; multigame accounts need a CS signal; dedupe vs state.json, items.json,
  linked article URLs (e.g. HLTV tweet linking an HLTV article we already have) and similar titles.
  Team-account clips/hype pass the filter: prefer core accounts, use team posts for official roster/statement news.
  Add an account: x get_users_by_usernames (one batch call), append {username,id,group,multigame} to x_accounts.json and to the query.
- `--no-source`: omit the source line. Use it for match result posts and match-of-the-day announcements.

Live in-match events (live.py; state: live_events; nothing is posted):
  python3 live.py bo3:<id> --brief [--dry-run] [--x-file <x dump> ...] [--late] [--no-map-start]
  -> NEW events since last run: map_started, pistol (r1/r13, winner+side), ace (player), half_time (score+sides), map_finished (map, score, OT, series),
     x_highlight (ace/pistol/clutch/map posts from ESLCS/HLTVorg/BLAST/PGL + the two teams' accounts).
  Sources: bo3 REST match/games (map end + score, ~minutes); bo3 live websocket wss://live.bo3.gg/ws/live_game/<id>
  recorded by `python3 live.py bo3:<id> --listen --minutes 330 &` into live_buf/<id>.jsonl (real-time rounds/kills,
  ONLY when bo3 has live coverage: match parsed_status != "waiting"; also REST /live/matches/<id>/last_snapshot);
  --late = demo-parsed rounds (exact pistols/aces, but only 2-3 h after the map ends, flagged late=true).
  HLTV (site + scorebot-lb.hltv.org) is 403 (Cloudflare challenge) from this box - not bypassed.
  Liquipedia (lp_live.py, used automatically by live.py while the match is "current"; --no-lp to skip): LP editors fill
  t1firstside + first-half splits (t1t/t1ct/t2t/t2ct) in the tournament page wikitext live -> half_time event
  (source "Liquipedia", sides known) ~1-5 min after round 12, and finished map scores (map_finished fallback, same id,
  bo3 REST wins if both). No per-round live score anywhere box-accessible (bo3 live feed only when parsed_status !=
  "waiting"; Ensigame shows map scores only after the match; PandaScore/GRID need API keys). Half-time of an already
  finished map is not emitted. API etiquette: identifying UA, gzip, <= 1 req/2 s, query not parse (3 req on first call
  per tournament for page discovery, cached in state live_events[key].lp_titles; then 1 req per run).
    python3 lp_live.py <bo3 id>      # debug: LP maps with half/final oriented team1:team2 of bo3
  Telegram cross-check (tg_scan.py, channels in tg_channels.json, public t.me/s previews, read-only):
    python3 tg_scan.py --teams Vitality MOUZ --minutes 60                  # relevant posts + score tokens
    python3 tg_scan.py --teams Vitality MOUZ --minutes 30 --confirm 7:5    # exit 0 if 1 reliable or 2 channels
  @newcsgo posts informal half-time flashes ("5:1 ➡️ 5:7"); @hltvtelegram posts map results (often no round score).
  Use TG only to cross-check, never as the single source of a score.
  X fallback for a live match: x search_posts_all, query
    (from:ESLCS OR from:HLTVorg OR from:<team1 handle> OR from:<team2 handle>) -is:retweet -is:reply,
    start_time = match start - 20 min, write_to_file: true, then --x-file <dump>.

Image cards (card.py, PIL; nothing is posted). 1280x720, channel style (charcoal grid, orange crosshair, Exo 2 Black Italic,
de_csnews wordmark bottom-right). Fonts are in fonts/ (Exo 2 variable, OFL). Team/event logos from bo3 URLs are cached in
cards/logo_cache (download failure -> text only; --no-logos to skip).
  python3 card.py TEMPLATE --json data.json -o cards/out/x.jpg      # --json - = stdin; .png or .jpg by extension
  Templates (field names = matches.py output, so a match dict can be passed as-is):
    result       match dict: event, stage, format, team1, team2, score "2:1", maps [{map, score, winner}], team1_logo, team2_logo, date_msk
    day_results  {date, matches:[match dicts]} (grouped by event) or {date, groups:[{event, matches}]}; overflow -> "+ ещё N"
    announce     {date, matches:[{start_msk, team1, team2, format, event, stage_short|stage}]}; footer "Время московское"
    match_start  match dict (+ team ranks shown as "#3 в мире"); (poll footer only when POLLS_ENABLED)
    poll_result  poll.py --stop/--results JSON, or {question, options:[{text, votes}], correct, guessed_percent}
    weekly       {title?, period "5–11 октября", lines:[<=5 strings]} (each line wraps to max 2 lines)
    tournament   matches.py --tournaments entry, or {name, start_date, end_date, prize, teams_count, location, tier, logo}
  Text in cards: write Russian stage names (stage_short) / locations yourself if wanted; long names shrink, then wrap/…
  Samples: cards/samples/*.jpg (data in cards/samples/data/*.json).
  Post with a card: python3 post.py ann-2026-10-12 --text-file t.txt --url <url> --source bo3.gg --image cards/out/x.jpg --no-source --no-mark
  (post.py --image takes a URL or a local file path; local files are uploaded via multipart sendPhoto, caption <= 1024.)

Channel stats (stats.py; for the weekly owner report, NOT posted):
  python3 stats.py [--days 7] [--summary-only] [--no-save]
  Subscribers: getChatMemberCount(@de_csnews); each run appends to stats.json history -> weekly growth vs the newest
  snapshot >= 6.5 days old (partial=true until a week of history exists). Posts: scrapes https://t.me/s/de_csnews
  (?before=<id> pagination) for the last N days: views, reactions, type (news/live/announce/results/poll/poll_result/weekly/
  tournament/welcome, inferred from text), top 5 by views, average views, by_type averages, summary_ru (short Russian text).
  Baseline 2026-10-11 15:47 MSK: stats_baseline_2026-10-11.json.

## Proofreading checklist (mandatory before every send)
0. Read LESSONS.md, then run `python3 check_post.py draft.txt` (add `--text` for text posts) on every draft. Fix every FAIL
   (exit 1: score contradicting channel history, links/domains/@handles, hashtag count, length, broken HTML) and verify
   every WARN by hand (counts/ordinals/records against the current HLTV/Liquipedia profile, headline repeats).
   Published post: `python3 check_post.py --post N`. Read-only, posts nothing.
1. Run post.py with `--dry-run` first and reread the rendered caption exactly as subscribers will see it (card text too, if any).
2. Facts: every name, score, map, number and date matches the source; nothing invented; ambiguous English captions are rendered only as far as they are certain; rumors are labelled as rumors.
3. Language: natural Russian as a native CS fan writes it (e.g. «вытащили раунд 2 против 4», «клатч 1 в 3», «взяли пистолетку», «камбэк»), no literal translation, no calques, correct grammar, cases and punctuation, no typos.
4. Sense: the headline and body don't repeat each other, nothing contradicts itself, the post makes sense to someone who didn't watch.
5. Format: bold headline, at most one emoji in it, hashtags with common names, no links, within length limits.
6. Score consistency: scores of matches/maps already covered in the channel must match our earlier posts. When sources disagree (HLTV vs bo3.gg vs Liquipedia etc.), the channel history (https://t.me/s/de_csnews) is the source of truth, because those scores were checked live; if the channel has no score for it yet and sources disagree, omit the score rather than guess.
7. Fix anything found, dry-run again, and only then send.

Mistake rule (standing user requirement): whenever a mistake is found (by the user, by a routine or while editing), fix the
post (editMessageCaption/editMessageText), add a LESSONS.md entry (date, post id, what went wrong, root cause, preventing check),
and add or extend a check in check_post.py or in this checklist so it cannot repeat.

## Live collector (collector.py + live_queue.py; pre-detects live events, posts NOTHING)
Background process that pre-detects live events every ~60 s so the 5-minute live routine only verifies, proofreads and posts.
It runs ONLY while needed: a notable match (matches.py --today) is live or starts within 15 min, inside 10:00-23:59 MSK.
When no notable match is live/upcoming it exits by itself ~10 min after the last one finished (grace). It never idles.
  ./ensure_collector.sh        # routine calls this every run: starts collector.py detached (setsid, own session) only if
                               # a notable match is live / starts within 15 min; restarts it if heartbeat > 3 min old;
                               # otherwise prints "not started: ..." and does nothing (exit 0 either way)
  python3 collector.py --status | --stop | --check   (--check: exit 0 if a notable match is live/soon)
  Single instance: flock on collector.lock + collector.pid. Heartbeat: collector_heartbeat.json (last_tick_msk, status
  polling|waiting_for_start|grace|error|stopped, needed, polled, last_added, queue_pending, last_error, stop_reason).
  Log: logs/collector.log (rotating 1 MB x 3); stdout/crash: logs/collector.out; own detection state: collector_state.json.
Per tick (for matches that are live or past their start time): bo3 REST match/games (match/map start, map end + score,
match end), bo3 websocket buffer (it spawns `live.py bo3:<id> --listen` itself when bo3 has live coverage) + REST snapshot
(pistols, aces, half-time), Liquipedia wikitext via lp_live (half-time with sides, final map scores; action=query only,
>= 2.1 s between requests, revisions cached 55 s, page titles cached, backoff 1-30 min on errors / 5-60 min on 429),
Telegram previews of tg_channels.json channels with live:true (moments + score flashes) or reliable:true (score
confirmation). NO X reads (X is only reachable from the routine). The schedule is re-read every 5 min.
It never writes state.json; it dedupes against state.json live_events[key].seen (+ match_results for match_finished /
tg tips) and against the queue itself.

Queue: live_queue.json, a list (append-only; unposted items may get updated in place when more evidence arrives):
  {"id": "bo3:130754|map2:half", "event_id": "map2:half" (= live.py id, the one marked in state live_events seen),
   "type": match_started|map_started|pistol|ace|half_time|map_finished|match_finished|tg_moment,
   "match": {key, team1, team2, event}, "map", "map_number",
   "scores": {"score": "7:5" (half_time: team1:team2; map_finished: winner first), "series": "1:0" (team1:team2),
              "by_source": {...} when several sources gave a score},
   "data": {all live.py event fields: winner, loser, sides, overtime(s), round, player, poll_key, maps...;
            tg_moment: kinds [ace|quad|clutch|outnumbered|overtime|pistol|comeback|streak|map_point|half_time|map_result],
            channel, text, posted_msk},
   "sources": ["Liquipedia", "bo3 REST", ...], "evidence": [{source, url}],
   "confirmed": true|false, "conflict": "sources disagree: bo3 REST 16:12, Liquipedia 12:12" | null,
   "detected_at": MSK ISO, "updated_at", "status": "new"|"posted"|"skipped", "posted_at"}
  confirmed rules: half_time/map_finished = Liquipedia score present and no disagreement, or 2 sources agree, or TG
  confirm (1 reliable channel or 2 channels); any disagreement -> false + conflict. match_finished is false while any of
  its map scores is unconfirmed. tg_moment is ALWAYS false (a tip from an informal channel: verify via X / other source).
  python3 live_queue.py --pending --brief [--key bo3:ID]      # unposted events, oldest first
  python3 live_queue.py --mark posted 'bo3:ID|map2:half' ...  # after send: queue status + state.json live_events seen
  python3 live_queue.py --mark skipped 'ID' ... [--note why]  # not worth a post / merged into another post (also -> seen)
How the live routine consumes it (every run) — USAGE CUT (14.10.2026, LESSONS 22):
  0. FIRST: `python3 live.py --gate`  (single-flight + idle fast-path).
       IDLE  -> print + exit 0 within seconds. Do NOT read X, do NOT scan TG tips, do NOT start the collector,
                do NOT call news_queue, do NOT post. Silent. (no notable match live and none starts within ~15 min)
       BUSY  -> print + exit 0. Another live run still holds `.live.lease` (<20 min) or `.live.lock`. Never run two.
       ACTIVE -> take the lease, continue. At the VERY END of a successful ACTIVE run: `python3 live.py --release`.
                (If the run is killed / times out, the lease auto-expires after 20 min; do not --force-gate casually.)
     Also: every `live.py bo3:<id> ...` takes an exclusive flock on `.live.lock` (except --listen / --list / cs: reads);
     a second concurrent call exits 0 with "BUSY: another live.py run holds .live.lock".
  1. ./ensure_collector.sh   2. python3 live_queue.py --pending --brief -> handle these first (they are pre-detected).
  3. Still verify scores per the existing rules: Liquipedia or 2 channels; for matches already covered, the channel
     history (t.me/s/de_csnews) wins; conflict/unconfirmed -> confirm or post without the exact score (LESSONS 2, 7).
  4. Still do the X reads for highlights (live.py --x-file) ONLY on ACTIVE runs; tg_moment items are tips to match against X / bo3.
  5. Card + check_post.py + proofreading checklist + post.py --dry-run, send. (POLLS_ENABLED=False; post.py also
     refuses DUPLICATE of a similar post from the last 3 h — on DUPLICATE mark the event posted and move on.)
  6. Immediately: python3 live_queue.py --mark posted <ids> (this also writes state.json live_events seen; for events
     handled without the queue keep using live.py's normal state). Skip junk with --mark skipped.
  7. `python3 live.py --release`  (drop `.live.lease`).
  If the collector is not running (no match live), the routine falls back to its normal live.py flow — but ONLY after
  an ACTIVE gate; never skip the gate.
  Before stopping on «nothing live» (ACTIVE but queue empty / match just ended): check daily_extra.json (today) and
  state live_events with `start` but no `end` (overnight carry-over, LESSONS 17) -> post result card on the first
  morning run (no poll / poll_result: polls are off). Then --release.
  Tests (no posting): ./ensure_collector.sh --sim-schedule test/sim_schedule.json --queue test/queue.json
     --cstate test/cstate.json --ignore-posted --grace-min 1 --tick-sec 20 --ignore-window   (replays Vitality-MOUZ)

## Catch-up bursts (lesson 23)
- `live_queue.py --pending` collapses stale events per match: only the newest state is shown (e.g. map1 end + map2 end
  + map3 start pending at once -> only `map3:start`; if `match_finished` is pending -> only the result). Superseded
  events are auto-marked `skipped` ("superseded (catch-up collapse)"). Put their scores into the kept post's text
  ("разошлись по одной: 16:14 и 13:9"). `--no-collapse` shows everything (debug). Logic: `live.collapse_stale()`.
- `post.py` waits until >= 75 s (env LIVE_GAP_SEC) have passed since the previous channel post (inside the send
  lock, so parallel senders line up). `--no-gap` only for a genuinely urgent single post. `live.py --wait-gap` = the same wait standalone.
- Cards for leagues without map names (cybersport.ru gives «Карта N"): card.py map_start prints «Старт решающей
  карты» / «Старт N-й карты», never «Старт 3-й карты: Карта 3»; half_time omits the map box.

## Event rules (events.json) — live_tiers, media/streamer leagues, demoted events
- `live_tiers` (single reversible flag):
  - `"top"` (current, usage save 14.10.2026): live/collector cover **only** normal notable matches (S/A, or B with
    top teams). `notable_events` (media/streamer) and `demote_events` (Stake Pulse) are **excluded from live
    entirely** — even Stake Pulse with a top-10 team. `collector.py` calls `matches.py --today --live`; gate returns
    IDLE when only media/Stake Pulse are on. Set to `"all"` to restore live coverage of media + demoted-with-rank.
  - Daily announce and daily results do **not** pass `--live`, so streamer league / Stake Pulse can still appear in
    the morning announce and evening recap (`media_league.py` / `matches.py --today` / `--results`). Put missed live
    streamer results into `daily_extra.json` for the recap.
- `notable_events`: media/streamer leagues via cybersport.ru (`media_league.py`, keys `cs:<id>`). Now: BETBOOM
  Streamers Battle CS #6 (12–17.10.2026). Added to matches.py unless `--live` + `live_tiers=top`. New league: add
  tournament_id + match id range, start/end dates.
- `demote_events`: regex on event name. With `live_tiers=all`: covered live only if a team is ranked ≤ `min_rank`.
  With `live_tiers=top` + `--live`: hard-excluded. S/A-tier never demoted. Now: Stake Pulse (min_rank 10).
- Media-post style (when live is on again): short, по-стримерски, по никам капитанов; хэштеги из `hashtags` +
  #de_csnews; без строки источника. Map NAMES from cybersport.ru can be wrong — scores OK, drop names if unsure.

## Daily results: extra items
Before writing the daily results post, read `daily_extra.json`; if its date is today, include its items (they weren't posted live). Delete/ignore after use.

## Cron: morning announce + daily results WITHOUT AI (since 14.10.2026, usage save)
GitHub Actions (раздел в начале файла) ведёт анонс, итоги и лайв, когда `POSTING_ENABLED=true`. Ветку состояния Actions бокс не видит, поэтому `sched/` для этих трёх задач к тому моменту должен быть остановлен — иначе канал получит два поста.
SINCE 17.10.2026 THE SCHEDULER IS sched/ (NOT Debian cron). Why: box restarts kill every process, and a box
migration (17.10 10:19) restores only /workspace + /home/box onto a fresh image -> the apt `cron` package, crontab and
/var/spool/cron are GONE. Cron also logs nowhere (no syslog), so failures were silent. Do NOT re-enable cron jobs:
two schedulers = two runs (guards would block a dup post, but don't rely on it). Crontab of box is empty on purpose.
  sched/ensure.sh     idempotent: starts watchdog.sh (setsid nohup supervised loop) -> scheduler.py if not running or
                      tick > 3 min old. Prints "scheduler OK ..." / "scheduler STARTED ...". ANY agent may run it.
                      Called automatically by `live.py --gate` (silently) and by ~/.bashrc (interactive shells).
  sched/jobs.json     jobs (MSK, re-read every 20 s): heartbeat */5 -> logs/sched_heartbeat.txt; announce 10:04
                      (catch-up until 12:00); results 23:34 (catch-up until 23:59, same day only); collector */5 10-23
                      (./ensure_collector.sh, posts nothing). Each slot runs once (sched/state.json), even across restarts.
  logs/scheduler.log  RUN/DONE/CATCHUP/TIMEOUT lines; logs/scheduler_tick.json = last tick; logs/sched_<job>.out.
  NOTHING on the box starts at boot. After a box restart the scheduler is down until some agent/routine runs
  (live.py --gate or sched/ensure.sh). Missed slots inside the catch-up window run late; outside it they are skipped.
Old cron layout (reference only):
`crontab -l` (user box, CRON_TZ=Europe/Moscow):
  4 10 * * *   run_auto.sh announce   -> announce_auto.py  (matches.py --today, not finished, demote_events skipped)
  34 23 * * *  run_auto.sh results    -> results_auto.py   (matches.py --results --hours 24, finished, not recapped yet)
  */10 * * * * heartbeat -> logs/cron_heartbeat.txt  (proof cron is alive)
run_auto.sh loads the token from `.cron.env` (chmod 600, never print/commit it) and logs to logs/announce_auto.log /
logs/results_auto.log. Both scripts: single-instance flock, card.py card (announce / day_results; card failure ->
text-only), `check_post.py` (any FAIL -> abort, nothing sent), post.py --dry-run, then post.py (dup check, image
validation, send lock, 75 s gap) with --no-source. No matches -> nothing posted. State: auto_state.json
(announced_dates, results_dates, recapped_keys) -> one announce / one recap per day; `--force` to override.
  python3 announce_auto.py --dry-run [--date YYYY-MM-DD]     # prints text + card path, sends nothing
  python3 results_auto.py --dry-run [--hours 24]
Text rules in auto_lib.py: event display names/hashtags (EVENT_RULES; #CS2 + event tags, max 4, post.py adds
#de_csnews), team short names (TEAM_SHORT, "Team X" -> X), stages (Group A -> Группа A, unknown -> omitted),
TBD-vs-TBD slots collapsed into "… — пары определятся позже", results winner-first, streamer-league map names
omitted (cybersport.ru names unreliable). New tournament -> add an EVENT_RULES line, otherwise the tag is auto-built.
The AI routines for 10:04 announce / 23:34 results must stay PAUSED, or the channel gets two posts.
check_post.py --since-hours N (results_auto passes 30): compare scores only with posts of the last N h (17.10:
false FAIL on 9z-Aurora opening 14.10 vs decider 16.10 rematch would have aborted the recap).
check_post.py: recap lines "A 2:1 B (...)" take teams from that line only (fix of a false score FAIL, 14.10).
