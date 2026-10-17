# LESSONS: mistakes log (read before every post)

Rule: when a mistake is found (by the user, a routine or an edit), (1) fix the post, (2) add an entry here with
the root cause, (3) add or extend a check in check_post.py or the README Proofreading checklist.
Format: date · post · what went wrong · root cause · check that now prevents it.

1. 2026-10-11 · post 33 · "третья MVP-медаль ZywOo в 2026" (correct: sixth, HLTV profile lists 6 in 2026; edited 21:35 MSK).
   Cause: count copied from an older article (IEM Rio, when it was his third), not the current state.
   Check: check_post.py flags ordinals/counts/records (WARN «counts»). Career/season counts must be verified against the
   CURRENT primary profile (HLTV player/team page or Liquipedia), never an older article.
2. 2026-10-11 · posts 33/34 · map scores of the EPL S24 final disagreed between sources (HLTV article text Cache 13-7 /
   Inferno 13-8 vs its own scoreboard and bo3.gg 13:8 / 13:7), so the weekly recap left them out at first.
   Cause: article prose is error-prone; no rule for which source wins.
   Check: prefer scoreboard / Liquipedia over article text; for matches already covered, the channel history
   (t.me/s/de_csnews) wins. check_post.py FAILs on a map score that contradicts recent channel posts (README item 6).
3. 2026-10-11 · post 22 · the 2v4 round was misdescribed (sense error; corrected to «ZywOo и ropz вытащили раунд в
   ситуации 2 против 4»).
   Cause: an English live caption was translated literally, without rereading for meaning.
   Check: checklist items 3–4 (reread for meaning as someone who didn't watch); check_post.py WARNs on headline/body repeats.
4. 2026-10-11 · message 13 · duplicate live map post (deleted).
   Cause: the event was not marked in state before the next 5-minute run, so a second run posted it again.
   Check: mark the event/match key in state.json (live_events / matches.py --mark) right after a successful send, and check
   state plus the last channel posts for the same event before sending.
5. 2026-10-11 · early news posts · source labels like «Dust2.us» and «X / @HLTVorg» became links in Telegram.
   Cause: raw domain/@handle text gets auto-linked.
   Check: post.py clean_label() (HLTV, Dust2) + check_post.py FAILs on domains, @handles and URLs in the draft.
6. 2026-10-11 · live map post · a map result went out as text only and was later replaced by a card post.
   Cause: the card step was skipped under time pressure.
   Check: live map_finished/result posts always use card.py (map_result/result) + post.py --image; dry-run shows the card.
7. 2026-10-11 · live Dust2 result · bo3 REST showed a stale 8:11 while the map was already in overtime.
   Cause: a single live source can lag or freeze.
   Check: cross-check final scores (bo3 REST + Liquipedia + tg_scan --confirm); if they disagree or are unknown, post without the
   exact score rather than guess (see live_pending.json note).
8. 2026-10-11 · half-time cards · "CT"/"T" text labels on the score card (user asked to drop them).
   Cause: design choice not checked with the user.
   Check: card.py half_time shows sides by score colour only; look at the rendered card before every send.
9. 2026-10-11 · post 8 · «BLAST.tv Major Austin» in the body was auto-linked to http://BLAST.tv/ (found by check_post.py,
   NOT yet edited in the channel; fix: «BLAST Austin Major»).
   Cause: clean_label() only cleans the source line; brand names with a TLD in the body (BLAST.tv, Dust2.us) still link.
   Check: check_post.py FAILs on any domain-like token in the whole draft, body included.
10. 2026-10-12 · Stake Pulse Beat III (Inner Circle–Sangal, NiP–Betclic) · lp_live found no Liquipedia match, so no
    Liquipedia half-time/map scores were detected for these matches (the half-time of Inner Circle–Sangal map 1 was never queued).
    Cause: LP uses short team templates (TeamOpponent|nip, |ic, |apogee) that team_match() didn't map to the bo3 names
    (Ninjas in Pyjamas, Inner Circle, Betclic); also «{{Abbr/CEST}}» was cut at «}» by parse_match, so match times were read as UTC.
    Check: lp_live.team_match() now uses LP_ALIASES + acronyms; parse_date accepts the cut tz. When a live match is running,
    `python3 lp_live.py <bo3 id>` must NOT print null; if it does, add the alias to LP_ALIASES before relying on LP.
11. 2026-10-12 · Inner Circle–NiP (winners' match, Stake Pulse Beat III) · `lp_live.py 131323` printed null although LP had
    the match, so no Liquipedia half-time/map scores would have been detected.
    Cause: the new match had no lp_titles cache and LP's search for the tournament name now ranked a team page (JiJieHao)
    and the qualifier subpages first, so the main page «Stake Pulse/Beat III» was never fetched.
    Check: lp_live.find_match() now reuses lp_titles cached by any other match of the same tournament in state.json and
    derives the parent page from subpage hits («X/Qualifier/1» -> «X»). Rule 10 still applies: null while live = fix first.
12. 2026-10-12 · Inner Circle–NiP Dust2 half · half_time 6:6 was never queued by the collector; live routine posted it as #58 after noticing it was in LP but missing from the channel.
    Cause: collector_state.json for bo3:131323 (and sibling Stake Pulse matches) cached bad lp_titles from LP search (JiJieHao / Qualifier/1) — same root as lesson 11 — so collector never saw the main page half score. Separately, `live.py` without `--dry-run` marks new events as seen on detect; a probe run can mark mapN:half before a post exists.
    Check: when a Stake Pulse (or any) match is live, collector_state matches[*].lp_titles must be the main tournament page (e.g. «Stake Pulse/Beat III»), not team/qualifier hits; if `lp_live.py <id>` works but the queue stays empty for half/map end, fix collector_state lp_titles. Probe with `live.py --dry-run` (or post immediately after a non-dry detect). Channel history wins over seen when deciding whether a half/map post already went out.
13. 2026-10-12 · check_post.py false FAIL on NiP 2:0 Inner Circle result · Dust2 13:11 flagged as contradicting post 49 Dust2 13:6 (IC–Sangal) because #StakePulseBeat counted as a shared «team» tag with #InnerCircle (intersection size 2).
    Cause: GENERIC_TAG did not treat event hashtags like StakePulseBeat as non-teams.
    Check: GENERIC_TAG now also matches pulse/beat/stake/championship/invitational/frag/thunderpick/qualifier. Score compares still need ≥2 overlapping *team* tags; event hashtags must not count.

14. 2026-10-12 · rematch IC–Sangal Dust2 · check_post FAILed 16:12 OT (decider) vs post 49 Dust2 13:6 (opening, same teams same day).
    Cause: score compare keys only on map + ≥2 team tags; a finished multi-map recap of an earlier series collided with a live single-map post of the rematch.
    Check: check_post.py skips channel posts that contain ≥2 map scores when the draft itself has only one map score (series recap ≠ live rematch map). Still FAILs if two single-map posts for the same map+teams disagree.


15. 2026-10-12 · media live Aunkere–StRoGo · map_start for Nuke (map 3) was never queued after Anubis ended; Anubis map_start was also skipped.
    Cause: `media_league.poll` only emitted match_started / map_finished / match_finished, not map_started, so the collector had nothing to queue for maps 2+.
    Check: media_league.poll now emits `mapN:start` for the first unfinished map when N>=2 (series score so far). Live routine must post the map_start card for media matches the same way as for bo3.

## 2026-10-12 — стримерская лига вместо Stake Pulse
- Ошибка: live освещал B-tier Stake Pulse (команды #18–20 проходили фильтр «B-tier + топ-20»), а аудитории интереснее
  шедший параллельно медиатурнир (BETBOOM Streamers Battle CS #6), которого нет на bo3.gg/Liquipedia.
- Проверка: каждое утро при анонсе смотреть cybersport.ru/tournaments/cs2 и TG-каналы на медиатурниры; если идёт —
  добавить в events.json `notable_events`. Список демотированных событий — `demote_events`.
- Проверка: названия карт с cybersport.ru сверять со статьёй/трансляцией; при расхождении писать счёт без названий карт.

16. 2026-10-12 · media live Aunkere–SEREGA PIRAT · queue falsely had `map2:start` (Anubis) while series was still 0:0 and map 1 (Nuke) unfinished.
    Cause: `media_league.poll` set `current_start_emitted` only inside `if n >= 2`, so an unfinished map 1 did not mark the «first unfinished» slot; map 2 at 0:0 then emitted `map2:start`.
    Check: `current_start_emitted = True` on the first unfinished map of any number (map 1 included). Live routine must skip `mapN:start` when series is still 0:0 and map 1 is not finished. `python3 media_league.py --poll cs:ID` for a just-started match should list only `|start`, not `|map2:start`.
    Follow-up (2026-10-12 23:59): marking the false `map2:start` as skipped also put it in state seen + kept the queue id,
    which would have blocked the REAL map 2 start post. Fixed by removing it from seen and renaming the skipped queue item
    (`#falsepos`). Check: when skipping a false positive whose event can still really happen later, do not leave its
    event_id in seen / the queue under the same id.

17. 2026-10-13 · overnight carry-over Aunkere–Серёга Пират (cs:10099325) · result posted only at 11:08 (#81), ~1 h after live coverage resumed at 10:01.
    Cause: the match finished after the 23:59 cutoff; morning live runs saw «no notable match live/finished since last run» and stopped before reading daily_extra.json, which held the carry-over instruction.
    Check: every live run, before stopping on «nothing live», reads daily_extra.json (date = today) and live_events for matches with `start` but no `end` (from yesterday too); a finished one gets its result card + poll close on the first run after 10:00. Map names from cybersport.ru without confirmation → «Карта 1/2» on the card, scores only.

18. 2026-10-14 · user asked to stop match polls.
    Change: common.POLLS_ENABLED=False; poll.py refuses sendPoll, card.py refuses poll_result, match_start card has no «голосуй» footer, live.py no longer hints «закрыть опрос». Older notes here (e.g. #17 «poll close») are superseded: no poll and no poll_result posts until the user re-enables them.

19. 2026-10-14 · Thunderpick World Championship 2026 (9z–Aurora) · `lp_live.py 131666` printed null before map 1, so no Liquipedia half/map scores would have been found.
    Cause: find_match() turned the search hit «Thunderpick/World Championship/2026/Europe/1» into the series page «Thunderpick/World Championship» (parts[:2]); its allpages list (20 max) is filled by 2023–2025 editions, so the real page «Thunderpick/World Championship/2026» was never fetched.
    Check: when the tournament name has a year, find_match() cuts subpage hits at that year segment and keeps only that year's pages. Fixing a bad cache: stop the collector, wait until `--status` shows running=false (it rewrites collector_state.json on exit), then clear matches[key].lp_titles and restart.

20. 2026-10-14 · Astralis–Sashi (bo3:131331) · result nearly missed: Mirage 13:9 (LP) was marked seen by a probe `live.py --brief` (no --dry-run) before any post, so the collector never queued map2:finished; live.py also printed «серия 1:1» because LP had Nuke as 12:12 (OT not filled in) and counted it as a finished draw.
    Cause: probe run without --dry-run (same as lesson 12); lp_live treats a tied regulation score as a finished map.
    Check: probe with `live.py --dry-run` only; a series score from live.py that contradicts the channel (post 117: 1:0) means LP is stale — take map winners from bo3 REST/channel, the map score from LP only if the map itself has no conflict. Posted as #120 after bo3 REST showed 2:0.

21. 2026-10-14 · HOTU–Astralis and Falcons–NiP · duplicate map results: Ancient 16:13 Falcons went out as #123 and #128, and Dust2 13:6 HOTU as #126 and #127 (#126/#127 in the same second, 17:29:52 MSK). Two live runs overlapped (one was slow or interrupted and the next started); each one saw the event as new and wrote and sent its own text. live.py only marks events seen after printing them, and post.py had no memory of what had been sent.
    Cause: no cross-run dedup at send time.
    Fix: post.py now takes an exclusive lock (sent_log.json.lock) around check+send+log and refuses ("DUPLICATE: …") to send when a post in the last 3 h has the same first line, or shares a score and ≥2 latin words (teams/maps) in its first line. Override with --force-dup only for a genuinely different post.
    Check: on DUPLICATE, don't force it; mark the live event / queue item as posted and move on.

21. 2026-10-14 · Falcons–NiP (bo3:131667) · collector queued map2:finished with серия 1:1 while channel already had Ancient 16:13 (1:0) and Dust2 13:6 made it 2:0.
    Cause: lp_live map_info treats OT maps as final=[12,12] (regulation only), so Ancient counted as a draw in the series; Dust2 win then became 1:1.
    Check: before posting map/match result, if LP series contradicts the channel (or a prior map_result with OT), take map winners from channel/bo3 and only the current map score from LP. Deciding-map finish → post `result` card (series 2:0), skip separate map_result. Do not trust live.py «серия» when any map has OT.

22. 2026-10-14 · live routine burned most of the daily agent limit (~170 wakes × full X/TG work even with no match)
    and overlapping runs caused duplicate map posts (#126/#127 HOTU Dust2, #123/#128 Falcons Ancient).
    Cause: the 5-min live routine always did full work (ensure_collector, news_queue, X reads, TG tip scans) and had
    no single-flight lock, so a slow/killed run and the next wake both posted the same event.
    Fix: `live.py --gate` / `--release`. IDLE (collector.py --check: no notable match live and none within 15 min) =
    silent exit in <1 s, no X/TG/collector/news_queue. BUSY = another run holds `.live.lease` (<20 min) or the
    flock `.live.lock` → exit 0. ACTIVE = take lease, do the usual work, `--release` at the end. Every `live.py
    bo3:<id>` also takes `.live.lock` (except --listen/--list/cs:). post.py still refuses 3 h near-dupes (LESSONS 21).
    Check: an idle wake must print `IDLE: …` and stop; two concurrent live.py must print BUSY; never skip --gate.

23. 2026-10-14 · Team Hooch–Team Ct0m (cs:10099330, BETBOOM Streamers Battle CS #6) · #131 (map1 16:14), #132 (map2 13:9), #133 (map3 start) all went out within 4 s at 17:35 MSK; #133's card title read «СТАРТ 3-Й КАРТЫ: КАРТА 3».
    Cause: cybersport.ru polls failed for ~1 h (DNS/SSL errors, collector restarts), then a catch-up run posted every queued event back to back; card.py t_map_start glued the placeholder map name «Карта 3» into the title.
    Fix: live.collapse_stale() + live_queue.py --pending auto-skips superseded events (only the newest per match, earlier scores go into its text); post.py spaces channel posts >= 75 s; card.py is_placeholder_map() -> «Старт решающей карты» / «Старт N-й карты», half_time drops the empty map box.
    Check: after an outage never post a chain of old map events; one post with the current state. Look at the card title before sending: no «Карта N» as a map name.

24. 2026-10-14 · Team Hooch–Team Ct0m · #132 map2 result card had a grey bottom half (truncated JPEG).
    Cause: overlapping live runs; one process read the card file while card.py was still writing it, so Telegram got a half-finished JPEG.
    Fix: card.py render() writes to `out.tmp` then `os.replace` (atomic). post.py validate_image() opens with PIL, calls img.load() (full decode) and rejects tiny/corrupt files with `BROKEN IMAGE: …` — send nothing.
    Check: before sendPhoto, local images must pass validate_image; a truncated jpg must abort, never go to Telegram.

25. 2026-10-14 · usage save: live covered BETBOOM Streamers Battle + Stake Pulse (even with top-10), burning agent
    limits; overlapping catch-ups also flooded the channel (#131–#133).
    Fix: `events.json` → `live_tiers: "top"`. `matches.py --live` (used by collector/gate) skips `notable_events`
    and hard-excludes `demote_events` (Stake Pulse with no min_rank exception). Daily announce/results omit `--live`
    and still see media + demoted-with-rank. Set `live_tiers` to `"all"` to restore previous live coverage.
    Check: with only streamer/Stake Pulse on, `live.py --gate` / `collector.py --check` must print IDLE / needed=[].
    In-progress media matches → stop live, put results in `daily_extra.json` for the evening recap.
