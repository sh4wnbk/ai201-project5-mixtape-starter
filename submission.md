# Mixtape Bug Hunt — Submission

## AI Usage

**1. This session (Claude Code) — codebase orientation, reproduction, and diagnosis.**
I used Claude Code to map the codebase (`app.py`, `models.py`, `routes/`, `services/`) before touching any bug, to actually reproduce all 5 bugs against seeded data (via `flask shell`-equivalent Python scripts, `curl` against the running dev server, and `pytest`) before fixing anything, to trace each route → service call chain, and to implement and commit the fixes below.

Two honest verify/course-correct instances from this process:

- The assignment's description of bug #2 (search returns duplicate rows) turned out **not to reproduce** in this environment. I checked the raw SQL directly and confirmed the join does fan out to 3 rows for a 3-tag song, but SQLAlchemy's `Query.all()` on a plain entity query auto-deduplicates by primary-key identity, so the duplication never reaches `to_dict()` — confirmed at the ORM layer and again over live HTTP (`GET /songs/search?q=a` returned 0 duplicated titles). I had to revise my root-cause write-up from "causes visible duplicates" to a latent/version-dependent risk, rather than restating the assignment's claimed symptom as observed fact.
- For bug #5, my first reproduction attempt (checking `nova`'s own "listening now" feed) showed no visible problem — nova's friends all happened to have a very-recent event masking their older one via the per-friend dedup logic. I had to check across all 5 seeded users before finding a view (`kenji`'s) where a 2-hour-old event from `nova` incorrectly appeared as "listening now," which is what actually justified the threshold fix.
- Separately, a baseline `pytest tests/` run before any fix surfaced an unexpected second playlist test failure (`test_playlist_returns_songs_in_order`) that I hadn't anticipated from the bug table alone — the reproduction step changed my understanding of the blast radius of bug #3 before I wrote its RCA entry.

**2. A separate prior investigation (different conversation) — trigram naturalness-scorer experiment.**
Before this session, a per-line "surprise" scorer (`surprise.py`) was built and trained on this repo's own code, then tested against the 5 known bug lines, followed by a formal A/B test (`ab_test.py`) of a "crosstalk" hypothesis (the idea that trigrams spanning a line boundary get credited to the wrong line, borrowed from an RF-EMF exposimetry crosstalk-calibration technique; the fix tested was resetting context at each line boundary).

Baseline result — scorer's top-flagged ("peak") line vs. the actual bug line, per file:

| File                 | Bug line | Peak (baseline) | Distance | Rank of bug |
| -------------------- | -------- | --------------- | -------- | ----------- |
| streak_service.py    | 72       | 68              | 4        | 3           |
| search_service.py    | 27       | 33              | 6        | 5           |
| playlist_service.py  | 66       | 29              | 37       | 14          |
| feed_service.py      | 13       | 49              | 36       | 6           |

(`notification_service.py` — the omission bug — wasn't in scope for this scorer; there's no line to flag when the bug is a *missing* line.)

A/B result (line-reset vs. baseline):

| File     | Baseline dist | Line-reset dist |
| -------- | -------------- | --------------- |
| streak   | 4              | 11 (worse)      |
| search   | 6              | 5               |
| playlist | 37             | 34              |
| feed     | 36             | 30              |

Mean distance moved 20.8 → 20.0 — negligible, noise-level. Streak actually got **worse** (peak slid from line 68 to line 61). **Conclusion: the crosstalk hypothesis was falsified.** Scores were tied at a saturation ceiling (~7 bits), not smeared by boundary leakage — on the streak file, the previous line's context was carrying real signal, and isolating it lost information. Per-line naturalness scoring brackets bugs but doesn't localize them precisely (playlist and feed — the "invisible" omission/threshold-style bugs — stayed far off in both versions); execution-based tracing was the correct next step.

**This scorer is explicitly not the method used to satisfy the RCA "how you found it" field below** — those entries reflect this session's actual route → service reading and reproduction, even in the cases where the scorer happened to flag a nearby line.

---

## Codebase Map

- **`app.py`** — Flask application factory (`create_app`). Registers 4 blueprints (`songs`, `playlists`, `users`, `feed`) under their respective URL prefixes and calls `db.create_all()`.
- **`models.py`** — SQLAlchemy models: `User`, `Song`, `Tag`, `ListeningEvent`, `Rating`, `Playlist`, `Notification`, plus association tables `friendships`, `song_tags`, and `playlist_entries` (the last of which carries extra columns: `position`, `added_by`, `added_at`).
- **`routes/`** — thin HTTP layer. Each route parses the request, calls exactly one service function, and catches `ValueError` to return a 404 or 400 with an error body. `routes/users.py`'s `GET /users/<id>` is the one exception — it calls `db.session.get(User, user_id)` directly and checks for `None` itself rather than going through a service.
- **`services/`** — owns all business logic and is responsible for calling `db.session.commit()`. Routes never touch the session directly (aside from the one exception above).
  - `streak_service.py` — listening streak increment/reset logic, and recording listening events.
  - `search_service.py` — song search by title/artist.
  - `playlist_service.py` — playlist creation and ordered song retrieval.
  - `notification_service.py` — creates and retrieves notifications; also owns `add_to_playlist` and `rate_song`, since both actions can notify another user.
  - `feed_service.py` — "friends listening now" (recency-filtered) and "activity feed" (unfiltered, most-recent-N).
- **`seed_data.py`** — populates 5 users with friendships, 10 tags, songs with varying tag counts, a mix of very-recent and hours-old `ListeningEvent`s, and 3 playlists.

**Traced data flow — rating a song:**
`POST /songs/<song_id>/rate` → `routes/songs.py:rate()` reads `user_id`/`score` from the JSON body → calls `notification_service.rate_song(user_id, song_id, score)` → validates the score range, looks up the `Song` and rating `User`, upserts a `Rating` row (update if one already exists for that user/song pair, else insert), commits, and (after this session's fix) notifies the song's original sharer via `create_notification()` unless the rater is the sharer themselves.

**Patterns noticed:**

- Routes are thin; services own logic and commit.
- The `db.session.get(Model, id)` + `raise ValueError(...)` pattern (services) or direct `None` check (the one route that skips a service) is the consistent way "not found" is represented; routes uniformly translate `ValueError` into a 404 or 400.
- Notification-worthy actions (`add_to_playlist`, and now `rate_song`) each guard against self-notification by comparing the acting user's ID to the song's `shared_by`.

---

## Bug Reports

### Issue #1 — Listening streak keeps resetting (`services/streak_service.py`)

**Reproduction steps:** Ran `pytest tests/ -v` before making any change. `tests/test_streaks.py::test_streak_increments_on_sunday` failed with `assert 1 == 2` — the test listens on a Saturday, then a Sunday, and expects the streak to go from 1 to 2, but it stayed at 1.

**Navigation strategy:** `routes/users.py` (`GET /users/<id>/streak`) calls `streak_service.get_streak`, which just reads the stored value — the actual mutation happens in `record_listening_event` → `update_listening_streak`. Read `update_listening_streak` top to bottom; its own docstring states the rule plainly: "If the user listened yesterday: streak increments by 1" with no day-of-week exception mentioned. The moment of confidence was line 73: `elif days_since_last == 1 and today.weekday() != 6:` — an extra `and` clause that isn't described anywhere in the docstring, immediately followed by an `else` that resets the streak to 1.

**Root cause:** When `days_since_last == 1` and `today.weekday() == 6` (Sunday), the added condition makes the `elif` evaluate to `False`, so execution falls through to `else: user.listening_streak = 1`. The bug is conditioned specifically on the day *after* a 1-day gap being a Sunday — every other day of the week increments correctly. Correct behavior requires dropping the day-of-week check entirely, since the function's own contract (and the underlying feature — "listen on consecutive days") makes no reference to weekday at all; a streak-increment rule that silently excludes one day of the week is not a valid implementation of "consecutive calendar days."

**Fix:** Removed `and today.weekday() != 6`, leaving `elif days_since_last == 1:`.

**Side-effect check:** Re-ran `tests/test_streaks.py` in full. `test_streak_increments_on_consecutive_day` (a non-Sunday Monday→Tuesday case) still passes and still increments, confirming the fix restored the general one-day-gap rule rather than special-casing Sunday, and `test_streak_does_not_double_count_same_day` / `test_streak_resets_after_skipped_day` (unrelated branches of the same function) are unaffected.

---

### Issue #2 — Same song shows up twice in search (`services/search_service.py`)

**Reproduction steps:** `pytest tests/test_search.py -v` at baseline showed `test_search_no_duplicates_multi_tag_song` **passing**, contradicting the assignment's description of this bug. I then queried the seeded 3-tag song "Crown Heights Anthem" directly: compiling and executing the raw SQL for the `outerjoin` query returned 3 rows, but `db.session.query(Song).outerjoin(song_tags, ...).filter(...).all()` returned a list of length 1. Confirmed again at the HTTP layer: `GET /songs/search?q=a` against the full seed set returned 13 results with zero duplicated titles.

**Navigation strategy:** `routes/songs.py` (`GET /songs/search`) calls `search_service.search_songs`. Read the function: the `.outerjoin(song_tags, Song.id == song_tags.c.song_id)` is never referenced in `.filter()` or `.order_by()` — it can only affect row multiplicity, not which rows match. Checked `models.py` and found `Song.tags` is already a proper `relationship(...)` consumed by `Song.to_dict()`, so the join wasn't needed to populate tags either. The confidence moment was comparing the raw-SQL row count (3) against the ORM `.all()` result length (1) for the same query — that isolated exactly where the fan-out was being absorbed.

**Root cause:** This is a latent bug rather than a currently observed one. The join fans the SQL result out to one row per matching tag, but SQLAlchemy's legacy `Query.all()` auto-deduplicates results by primary-key identity when the query selects only a single entity's columns (no extra columns from the joined table are pulled in) — so no duplicate `Song` objects currently reach `to_dict()`. Correct, predictable behavior requires the query not to depend on that implicit ORM dedup step at all, since the join serves no filtering purpose and its only effect is to put correctness at the mercy of an ORM implementation detail that could change (e.g., under a different SQLAlchemy version, or if the query were ever rewritten to pull in extra columns via Core-style `select()`).

**Fix:** Removed the `.outerjoin(song_tags, ...)` call and the now-unused `Tag`/`song_tags` imports.

**Side-effect check:** Re-ran `tests/test_search.py` — single-tag and no-tag songs still return exactly once. Separately called `search_songs("Anthem")` post-fix and confirmed the `tags` field for the 3-tag song still returns `['rap', 'hip-hop', 'boom bap']`, i.e. tag data is untouched because it's populated via the `Song.tags` relationship inside `to_dict()`, not via the removed join.

---

### Issue #3 — Last song in a playlist never shows up (`services/playlist_service.py`)

**Reproduction steps:** Baseline `pytest tests/ -v` showed two failures in `tests/test_playlists.py`: `test_playlist_returns_all_songs` (`assert 4 == 5`) and `test_playlist_returns_songs_in_order` (missing `"Track 5"` from the expected list) — I hadn't anticipated the second failure before running the suite.

**Navigation strategy:** `routes/playlists.py` (`GET /playlists/<id>/songs`) calls `playlist_service.get_playlist_songs`. Read the function: the query itself (join on `playlist_entries`, `order_by(asc(position))`) looks correct and matches the docstring's stated ordering rule. The confidence moment was the return line, `[song.to_dict() for song in songs[:-1]]`, directly contradicting the function's own docstring note: "This function returns all songs in the playlist."

**Root cause:** `songs[:-1]` unconditionally drops the last element of the already correctly-ordered list before serialization, regardless of playlist length. For every non-empty playlist, exactly one song — whichever is last by `position` — never reaches the caller, even though the query that built `songs` was correct.

**Fix:** Changed the return line to `[song.to_dict() for song in songs]`.

**Side-effect check:** Re-ran `tests/test_playlists.py` after the fix — all 3 tests pass, including `test_playlist_returns_songs_in_order` (the unanticipated second baseline failure), confirming the fix restored both correct count *and* correct order rather than just satisfying the count assertion. `test_empty_playlist_returns_empty_list` also still passes (an empty list sliced with `[:-1]` was already `[]`, so that path was accidentally safe before too). Beyond the suite, I manually built a single-song playlist and called `get_playlist_songs` — this is the edge case most likely to have broken differently than the 5-song test suggests, since `[:-1]` on a length-1 list returns `[]` (looks like "empty playlist" rather than "one song missing"). Post-fix, the single-song playlist correctly returns 1 song.

---

### Issue #4 — No notification when a friend rates your song (`services/notification_service.py`)

**Reproduction steps:** Picked a song shared by `darius` ("Block Party"), had `nova` (not the sharer) call `rate_song(nova.id, song.id, 5)`, and checked `get_notifications(darius.id)` before and after: 0 notifications both times.

**Navigation strategy:** `routes/songs.py` (`POST /songs/<id>/rate`) calls `notification_service.rate_song`. Read the function top to bottom — it validates the score, upserts the `Rating`, commits, and returns, with no call to `create_notification` anywhere. The module's own docstring states "Notifications are generated when friends interact with a user's shared songs," so I opened the sibling function `add_to_playlist` in the same file for comparison. The confidence moment: `add_to_playlist` has an explicit `if song.shared_by != added_by_user_id: create_notification(...)` block that `rate_song` has no equivalent of, even though rating is exactly the kind of "friend interacts with your shared song" event the module claims to cover.

**Root cause:** This is an omission, not faulty logic — `rate_song` simply never calls `create_notification`. The existing `add_to_playlist` pattern (guard against self-action, then notify `song.shared_by`) establishes what correct behavior looks like for this class of action; `rate_song` is missing the second half of that pattern entirely.

**Fix:** Added a `create_notification(user_id=song.shared_by, notification_type="song_rated", body=...)` call after the commit, guarded by `if song.shared_by != user_id`, mirroring `add_to_playlist`'s self-action guard.

**Side-effect check:** Added `tests/test_notifications.py` (see Regression Test section below) covering both the notify and self-rating-doesn't-notify cases. Also independently re-verified `add_to_playlist`'s own notification path — a different function, untouched by this fix — still fires correctly (`song_added_to_playlist`), confirming the shared `create_notification` helper wasn't affected by adding a second caller.

---

### Issue #5 — Friends Listening Now shows people from yesterday (`services/feed_service.py`)

**Reproduction steps:** Queried every seeded friend-listening-event with its age in hours. `get_friends_listening_now(nova.id)` returned the "correct-looking" 3 friends by coincidence — each of nova's friends also had a very-recent event masking their older one via the function's own per-friend dedup. Checking across all 5 seeded users found the actual visible bug: `get_friends_listening_now(kenji.id)` included `nova`, based on a listening event that was **2.05 hours old**.

**Navigation strategy:** `routes/feed.py` (`GET /feed/<id>/listening-now`) calls `feed_service.get_friends_listening_now`. Read the function: `cutoff = datetime.now(timezone.utc) - RECENT_THRESHOLD`, with `RECENT_THRESHOLD = timedelta(hours=24)` defined at module level. The confidence moment came from comparing that constant against the seed data's actual event-age distribution: true "just listening" events are all under 0.4 hours old, and the next-oldest batch starts at 2 hours — a 24-hour window swallows that entire 2h/10h/18h band, which is exactly why a friend's listen "from yesterday" was appearing as "now."

**Root cause:** `RECENT_THRESHOLD` defines the recency window used to decide "listening now," and at 24 hours it includes essentially all of the previous day's activity, not just genuinely-live listening. Any friend who listened within the last 24 hours — not just the last few minutes — is included in the feed.

**Fix:** Lowered `RECENT_THRESHOLD` to `timedelta(hours=1)`. This value was chosen (not guessed) because it sits cleanly inside the gap between the seed data's true-recent events (<0.4h old) and its next-oldest batch (2h+ old), so the fix is directly verifiable against the existing seed data.

**Side-effect check:** Read `get_activity_feed` and confirmed it does not reference `RECENT_THRESHOLD` at all — it's an intentionally unfiltered "most recent N events" feed per its own docstring. Called it directly for `kenji` after the fix and confirmed it still returns `nova`'s 2-hour-old listen (which `get_friends_listening_now` now correctly excludes), i.e. the two feeds now behave differently exactly as their docstrings describe, rather than the fix accidentally leaking into the unfiltered feed too.

---

## Regression Test (stretch, +1pt)

`tests/test_notifications.py` (new file, added alongside the Issue #4 fix) contains:

- `test_rate_song_notifies_sharer` — rates a song shared by a different user and asserts exactly one notification of type `song_rated` is created for the sharer.
- `test_rate_song_self_rating_does_not_notify` — rates a song shared by the same user who is rating it, and asserts no notification is created.

Both tests would have failed against the pre-fix code, since `rate_song` never called `create_notification` at all — `get_notifications(sharer.id)` would have returned `[]` in both cases, failing the `len(notifications) == 1` assertion in the first test (the second test would have passed vacuously pre-fix, but is included because a regression that reintroduces the self-notify guard specifically needs a test that can fail on its own). This test was written fresh for this issue rather than pointing at any of the pre-existing tests in `tests/test_streaks.py` / `test_search.py` / `test_playlists.py`, which already shipped in the starter repo and cover issues #1–#3 — using one of those for the stretch point would have credited a test I didn't write.

---

## Commit History

```text
$ git log --oneline bugfix/mixtape
eb9fd02 fix: tighten friends-listening-now threshold from 24h to 1h
278099a fix: notify song sharer when their song is rated
f5ea8f3 fix: return the last song in a playlist
3afcb85 fix: remove dead outerjoin from song search query
735c9f7 fix: remove spurious Sunday clause resetting listening streaks
2dfdeaa Add .gitignore file and update README with setup instructions
7b64551 initial commit
```

5 separate `fix:` commits on `bugfix/mixtape`, one per bug, each specific enough to identify the bug from the message alone.

**Fork:** <https://github.com/sh4wnbk/ai201-project5-mixtape-starter>, branch `bugfix/mixtape`.
