# Project 5: Mixtape Bug Hunt — Submission

**Author:** Cuiwen Wu
**Branch:** `bugfix/mixtape`

---

## Milestone 1 — Codebase Map

*Written before opening any issue file, as an orientation exercise. This section
describes how the app is built, not where the bugs are.*

### High-level architecture

Mixtape is a Flask + SQLAlchemy REST API for a social music app. It follows a
strict three-layer separation:

```
HTTP request
   │
   ▼
routes/*.py      ← thin controllers: parse input, call one service, format JSON
   │
   ▼
services/*.py    ← all business logic lives here
   │
   ▼
models.py        ← SQLAlchemy ORM models + association tables
   │
   ▼
SQLite (mixtape.db)
```

There is no controller logic in the routes and no HTTP handling in the services.
Every route immediately delegates to exactly one service function and wraps the
result (or a raised `ValueError`) into a JSON response. This is the single most
important pattern in the app: **if an endpoint misbehaves, the cause is almost
always in the service it calls, not in the route.**

### Main files and their roles

| File | Responsibility |
|------|----------------|
| `app.py` | Flask application factory (`create_app`). Instantiates the shared `db = SQLAlchemy()` object, configures the SQLite URI, registers the four blueprints under their URL prefixes (`/songs`, `/playlists`, `/users`, `/feed`), and calls `db.create_all()`. Everything imports `db` from here. |
| `models.py` | Defines all seven persistent entities and three association tables. This is the source of truth for the data model — read it first. |
| `routes/songs.py` | Endpoints for song search (`GET /songs/search`), song detail (`GET /songs/<id>`), rating (`POST /songs/<id>/rate`), and recording a listen (`POST /songs/<id>/listen`). |
| `routes/playlists.py` | Endpoints for creating playlists, listing a playlist's songs, and adding a song to a playlist. |
| `routes/users.py` | Endpoints for user detail, streak lookup, and notification listing / mark-as-read. |
| `routes/feed.py` | Endpoints for "Friends Listening Now" and the general activity feed. |
| `services/streak_service.py` | Records listening events and maintains each user's consecutive-day `listening_streak`. |
| `services/feed_service.py` | Builds the "Friends Listening Now" feed (recent, deduped per friend) and the unfiltered activity feed. |
| `services/search_service.py` | Case-insensitive song search across title/artist, joined to tags. |
| `services/notification_service.py` | Creates notifications when friends interact with a user's songs; also owns `rate_song()` and `add_to_playlist()`. |
| `services/playlist_service.py` | Playlist creation and ordered song retrieval. |
| `seed_data.py` | Drops and recreates all tables, then inserts 5 users (with friendships), 13 songs (deliberately with 0, 1, and 3+ tags), 3 playlists, listening events (recent + historical), streaks, and a sample notification. |
| `tests/` | Pytest suites for streaks, search, and playlists. |

### Data model (from `models.py`)

All primary keys are string UUIDs (`generate_uuid()`), not integers. Key entities:

- **User** — has `listening_streak` and `last_listened_at`, plus a self-referential
  many-to-many `friends` relationship (symmetric, stored as two rows per friendship
  in the `friendships` table).
- **Song** — shared by a user (`shared_by` FK). Has a many-to-many `tags` relationship
  through the `song_tags` join table. A song can have zero, one, or many tags.
- **ListeningEvent** — an append-only log row: one user listened to one song at
  `listened_at`. This table drives both streaks and the feeds.
- **Rating** — a user's 1–5 score for a song, with a `UniqueConstraint(user_id, song_id)`
  so a user can rate a song only once (re-rating updates the existing row).
- **Playlist** — has a `songs` many-to-many through the **`playlist_entries`**
  association table. That join table is richer than a plain link: it carries an
  explicit **`position`** integer, plus `added_by` and `added_at`. So playlist order
  is stored explicitly as a position column, not implied by insertion order.
- **Notification** — a `notification_type` + `body` message addressed to a `user_id`,
  with a `read` flag.

### Data flow trace #1 — a friend adds your song to a playlist → you get notified

1. `POST /playlists/<playlist_id>/songs` with `{song_id, added_by}` hits
   `add_song()` in `routes/playlists.py`.
2. The route validates that both fields are present, then calls
   `notification_service.add_to_playlist(playlist_id, song_id, added_by)`.
3. `add_to_playlist()` loads the `Song`, the adding `User`, and the `Playlist`
   (raising `ValueError` if any is missing). It appends the song to
   `playlist.songs` (which writes a `playlist_entries` row) and commits.
4. It then compares `song.shared_by` against `added_by_user_id`. If the adder is
   **not** the original sharer, it calls `create_notification(...)` targeting
   `song.shared_by` with type `song_added_to_playlist`.
5. `create_notification()` writes a `Notification` row and commits. The sharer can
   later read it via `GET /users/<id>/notifications`, which calls
   `notification_service.get_notifications()`.

This is the canonical "interaction → notification" pattern in the app, and it's
worth internalizing because the notification module is expected to follow this same
shape for *every* kind of interaction with someone's song.

### Data flow trace #2 — recording a listen updates the streak

1. `POST /songs/<song_id>/listen` with `{user_id}` hits `listen()` in
   `routes/songs.py`, which calls `streak_service.record_listening_event()`.
2. `record_listening_event()` loads the user, timestamps `now` (UTC-aware), creates
   a `ListeningEvent`, and then calls `update_listening_streak(user, now)`.
3. `update_listening_streak()` compares `now.date()` to the date of
   `user.last_listened_at`: same day = no change, one day gap = increment, larger
   gap = reset to 1. It then updates `last_listened_at`.
4. The same `ListeningEvent` row later feeds `feed_service`, which reads recent
   events to build "Friends Listening Now."

So one write (a listen) fans out to two read features: streaks and feeds. Both read
from the shared `ListeningEvent` log.

### Patterns I noticed

- **Routes are uniform and thin.** Every route follows parse → call one service →
  `jsonify`, and every service that can fail raises `ValueError`, which routes map to
  a 400 or 404. Business logic never leaks up into the routes.
- **Time is UTC-aware in code but the ORM columns are naive.** Services build
  `datetime.now(timezone.utc)`, but SQLAlchemy `DateTime` columns store naive
  datetimes. `update_listening_streak()` explicitly re-attaches `timezone.utc` to a
  loaded `last_listened_at` before comparing — a hint that timezone handling is a
  sensitive area.
- **`ListeningEvent` is the shared substrate** for three features (streaks, listening-now,
  activity feed). A change to how events are read affects all three.
- **Association tables carry semantics.** `playlist_entries.position` (ordering) and
  `song_tags` (many-to-many, so a song can appear multiple times across a tag join)
  are places where the *shape* of a query matters, not just its filter.
- **The seed data is intentionally shaped for testing** — songs with 0/1/3+ tags,
  events both inside and outside a 24h window, and users who listened today vs.
  yesterday. This tells me the fixtures are designed to expose edge cases.

---

## Bug Selection & Reproduction (Milestone 2)

I chose to reproduce **Issue #1 (streak)**, **Issue #5 (playlist)**, and **Issue #3
(search duplicates)** first, then pivoted from #3 to **Issue #4 (notifications)** for
the reason documented below. All reproduction was done read-only against the seeded
database — no application code was changed in this milestone.

### Note: why I pivoted away from Issue #3

Issue #3 reports that "the same song keeps showing up twice in search." I tried to
reproduce it before fixing. I ran `search_songs()` for six queries, including `q='e'`
which matches all five songs that carry 3 tags each (Crown Heights Anthem, Harlem
Renaissance, After Hours, Lagos to London, Frequencies). If the `outerjoin(song_tags)`
in `search_service` leaked its row multiplication, a 3-tag song would appear 3 times.

Observed: **every query returned each song exactly once** (`has_duplicate_ids=False`
for all six queries). The reason is that `search_service` uses the legacy
`db.session.query(Song)` interface, and legacy SQLAlchemy `Query.all()` de-duplicates
full-entity result rows by primary-key identity. The join still fans out to 3 rows for
a 3-tag song, but `.all()` collapses them back to one `Song` instance before the
service maps them to dicts. In this codebase version the reported duplicate does not
manifest, so I could not honestly reproduce it. Following the milestone's guidance
("if you can't reproduce a bug after a genuine attempt, try a different one"), I
substituted **Issue #4**, which reproduces deterministically.

---

## Root Cause Analyses

Each entry below is tied to a specific issue number and will be completed across the
remaining milestones with all five required fields: reproduction, navigation strategy,
root cause, fix, and side-effect check.

### Issue #1 — My listening streak keeps resetting

**1. How I reproduced it.**
The bug report is vague about *when* the reset happens, so I isolated the condition by
driving `update_listening_streak(user, now)` directly with controlled dates (a
read-only harness, no DB writes). I used three consecutive real calendar days in 2026:
Friday 6/26, Saturday 6/27, Sunday 6/28, Monday 6/29.

- **Sat → Sun** (streak 5, last listened Saturday, listens Sunday): a genuine
  consecutive day. Observed streak → **1** (should be 6). **Bug reproduced.**
- **Fri → Sat** (streak 5, listens Saturday): also consecutive. Observed streak → **6**.
  Correct — so the reset is *not* happening on every consecutive day.
- **Sat → Mon** (streak 5, listens Monday): a real one-day gap. Observed streak → **1**.
  Correct — this reset is legitimate.

The pattern is unmistakable: the streak resets **only when the new listen lands on a
Sunday**, even though the previous day (Saturday) was consecutive. That matches user
reports of a streak "keeps resetting" without an obvious cause — it silently breaks
once a week.

**2. How I found the root cause.**
I traced the "listen" action top-down from the route, not by guessing. `POST
/songs/<id>/listen` in `routes/songs.py` calls `record_listening_event()` in
`streak_service.py`, which creates the `ListeningEvent` and then delegates the streak
math to `update_listening_streak(user, now)`. Reading that function, the branch that
decides between increment and reset is:

```python
elif days_since_last == 1 and today.weekday() != 6:
    user.listening_streak += 1
else:
    user.listening_streak = 1
```

The `days_since_last == 1` half is correct — that *is* the definition of "consecutive
day." The extra `and today.weekday() != 6` clause is what made me stop. I confirmed
with a quick check (and by asking an AI to confirm Python's convention) that
`datetime.date.weekday()` returns `6` for **Sunday** (Monday is `0`). So on any Sunday,
`today.weekday() != 6` evaluates to `False`, the whole `elif` fails, and control falls
through to the `else` that resets the streak to 1. My reproduction harness already
showed Fri→Sat incrementing but Sat→Sun resetting — the `weekday() != 6` clause is the
one thing that distinguishes those two otherwise-identical consecutive-day cases. That
was the moment I was confident this was the cause and not just a suspicious line.

**3. The root cause.**
The consecutive-day branch carried an unnecessary and incorrect extra condition,
`today.weekday() != 6`. Because `weekday()` returns `6` for Sunday, this condition is
false every Sunday, so a listen on a Sunday — even one that directly follows a
Saturday listen — was routed into the reset branch instead of the increment branch.
Whether two days are consecutive has nothing to do with which weekday it is: `days_since_last == 1`
is already the complete and correct test. The `weekday()` clause introduced a spurious
weekly boundary where none should exist, so any user with an active streak lost it the
first time they listened on a Sunday.

**4. My fix and side-effect check.**
I removed the `and today.weekday() != 6` clause so the branch reads
`elif days_since_last == 1:`. This restores the intended rule — "one calendar day
since last listen ⇒ increment" — with no dependence on the weekday. It is the smallest
change that addresses the root cause; I touched nothing else.

Side-effect check: streak logic is a boundary problem, so I verified *both* sides of
every boundary rather than only the failing case. Using controlled dates I confirmed:
Sat→Sun now increments (6, the previously-broken case); Fri→Sat still increments (6, a
non-Sunday consecutive day, to prove I didn't just special-case Sunday); Sat→Mon still
resets to 1 (a genuine one-day gap must still reset — I specifically checked the reset
path wasn't collateral damage); same-day listen is still a no-op; and a first-ever
listen still starts at 1. I also ran the full streak test suite: the pre-existing
`test_streak_increments_on_sunday` (which failed before) now passes, and the other four
streak tests still pass.

### Issue #5 — The last song in a playlist never shows up

**1. How I reproduced it.**
I compared the number of `playlist_entries` rows in the database against the number of
songs returned by `get_playlist_songs()` for all three seeded playlists, and checked
whether the highest-`position` song was in the output.

| Playlist | Entries in DB | Returned by service | Highest-position song present? |
|----------|--------------|---------------------|-------------------------------|
| Late Night Vibes | 7 | 6 | No — "Free Throws" (pos 7) missing |
| Friday Energy | 7 | 6 | No — "Harlem Renaissance" (pos 7) missing |
| Study Mode | 7 | 6 | No — "Lagos to London" (pos 7) missing |

Every playlist returns exactly one fewer song than it contains, and in each case it is
specifically the song with the largest `position` value (the last one in playlist
order) that disappears. Consistent and deterministic. **Bug reproduced.**

**2. How I found the root cause.**
I followed the same route-first path: `GET /playlists/<id>/songs` in
`routes/playlists.py` calls `get_playlist_songs()` in `playlist_service.py`. The route
does nothing but `jsonify` the service's return value, so the "N−1" behavior had to
originate in the service. Reading `get_playlist_songs()`, the query itself is correct —
it joins `playlist_entries`, filters to the playlist, and orders ascending by
`position`, exactly matching the docstring "Songs are returned in the order they were
added." The final line is where it breaks:

```python
return [song.to_dict() for song in songs[:-1]]
```

The `[:-1]` slice drops the last element of the list. My reproduction had already shown
that the missing song is always the highest-`position` one; because the query orders
ascending by position, the highest-position song is exactly the *last* element of
`songs`, which is exactly what `[:-1]` removes. The docstring and the function's own
closing note both say it "returns all songs," which directly contradicts the slice —
that contradiction between stated intent and actual code is what made me certain the
slice was the defect and not the query.

**3. The root cause.**
`get_playlist_songs()` retrieves the correct, fully-ordered set of songs and then
discards the final one via the slice `songs[:-1]`. Since the results are sorted
ascending by `position`, the last element is always the song with the greatest
position — i.e., the most recently added / last song in the playlist. So the function
systematically omits the last song of every non-empty playlist. There is no condition
under which dropping that element is correct; it is a plain off-by-one truncation that
disagrees with the documented contract of the function.

**4. My fix and side-effect check.**
I changed `songs[:-1]` to `songs`, so the comprehension iterates the complete ordered
result. This is the minimal fix — the query was already correct, so only the erroneous
truncation needed to go.

Side-effect check (again a boundary bug, so I checked both ends): all three seeded
playlists now return their full count (7 = 7) with order preserved. I specifically
checked the small-input boundaries that a slice like `[:-1]` is most likely to break:
an **empty** playlist still returns `[]` (before the fix `[][:-1]` was also `[]`, so
this case never regressed — I confirmed it explicitly rather than assuming), and a
**single-song** playlist now correctly returns 1 song instead of 0 (this case was
silently broken before — one-song playlists appeared empty). I ran the full playlist
test suite: `test_playlist_returns_all_songs` and `test_playlist_returns_songs_in_order`
(both previously failing) now pass, and `test_empty_playlist_returns_empty_list` still
passes.

### Issue #4 — Notified when a friend adds my song to a playlist, but not when they rate it

**1. How I reproduced it.**
I picked a song shared by `simone` and had a different user (`nova`) rate it 5, then
counted notifications addressed to the sharer before and after.

- Before rating: sharer had **0** notifications of type `song_rated`.
- After `rate_song(nova, song, 5)`: sharer still had **0**. **Bug reproduced — no
  notification is created.**

For contrast, the seed data contains a working `song_added_to_playlist` notification,
and the code path that produces it (`add_to_playlist`) explicitly calls
`create_notification()`. So the platform clearly *can* notify a sharer about an
interaction — the rating path simply never does. This confirms the report: the
playlist-add notification works, the rating notification is silently absent.

*(Incidental finding while building the comparison harness: calling `add_to_playlist()`
live raises `NOT NULL constraint failed: playlist_entries.position`, because appending
through the `playlist.songs` relationship doesn't populate the `position`/`added_by`
columns the join table requires. This is a real latent bug but is not one of the five
listed issues, so it is out of scope here — noted for completeness.)*

**2. How I found the root cause.**
The project hint for this issue said the cause is architectural, not a typo, and to
compare the working notification path line-by-line with the missing one. Both live in
the same file, `notification_service.py`, so I put `add_to_playlist()` (the working
path) and `rate_song()` (the broken path) side by side. `POST /songs/<id>/rate` in
`routes/songs.py` calls `rate_song()`, and the route only formats the returned rating —
so any missing notification had to be the service's omission.

`add_to_playlist()` ends with an explicit notification step:

```python
if song.shared_by != added_by_user_id:
    create_notification(user_id=song.shared_by,
                        notification_type="song_added_to_playlist", body=...)
```

`rate_song()` validates the score, upserts the `Rating`, commits, and `return`s — and
then simply stops. There is no `create_notification` call anywhere in it. The moment I
lined the two functions up, the asymmetry was obvious: the working path has a
notification block and the broken path has none. That absence — not a wrong argument or
a typo — is the whole bug.

**3. The root cause.**
The app's architecture funnels every "someone interacted with your song" event through
`create_notification()`, and `add_to_playlist()` follows that convention. `rate_song()`
never adopted it: the function persists the rating but omits the notification step
entirely. So the behavior is exactly as reported — adding a song to a playlist notifies
the sharer (that code path calls `create_notification`), while rating the song notifies
no one (that code path has no such call). This is a missing-step defect, which is why
no amount of staring at the rating math would reveal it; the rating logic is correct,
it's just incomplete.

**4. My fix and side-effect check.**
I added a notification step to `rate_song()`, deliberately mirroring the exact shape of
the proven `add_to_playlist()` block so the two interaction paths stay structurally
consistent. After the commit, if `song.shared_by != user_id`, it calls
`create_notification()` with type `song_rated` and a human-readable body naming the
rater, the song, and the score. I placed it after `db.session.commit()` so a failed
rating write can't produce a phantom notification, and I reused the existing
`create_notification()` helper rather than writing new persistence logic.

Side-effect check — I verified specific behaviors that this change could plausibly
affect, not just that the app still ran:
- **Self-rating must not notify.** The `song.shared_by != user_id` guard mirrors
  `add_to_playlist`'s guard; I confirmed that when the sharer rates their own song, the
  `song_rated` count does not change. Without this guard, users would be notified about
  their own ratings.
- **The working path is untouched.** I confirmed the existing `song_added_to_playlist`
  notification is still present and its code path is unchanged — my edit is additive and
  confined to `rate_song()`.
- **Correct recipient and count.** A rating by another user produces exactly one
  notification, addressed to the sharer (not the rater), with the correct body.
- **Re-rating.** Because the block runs after the commit for both the insert and update
  branches, changing an existing rating also notifies. I consider this correct — a
  changed rating is a fresh interaction worth surfacing — and it matches
  `add_to_playlist`, which notifies on each call rather than only the first.
- Full test suite: 13 passed, 0 failed.

---

## AI Usage

I used an AI assistant as a navigation and explanation aid, not as a bug oracle. In
every case I located the suspicious code myself first, then used AI to confirm or
explain, then verified the answer against the code before acting.

1. **Confirming `datetime.weekday()`'s convention (Issue #1).** After I spotted the
   `today.weekday() != 6` clause, I asked the AI what `datetime.weekday()` returns and
   how it differs from `isoweekday()`. It explained that `weekday()` is 0-indexed from
   Monday (so Sunday = 6) while `isoweekday()` is 1-indexed (Sunday = 7). I verified this
   independently by printing `.weekday()` for known Saturday/Sunday/Monday dates in my
   reproduction harness (Sat=5, Sun=6, Mon=0) before concluding that `!= 6` was the
   Sunday special case. The AI explained; the harness proved it.

2. **Comparing the two notification paths (Issue #4).** I gave the AI the `rate_song()`
   and `add_to_playlist()` functions and asked what the structural difference between
   them was. It pointed out that `add_to_playlist()` ends with a guarded
   `create_notification()` call and `rate_song()` has no notification step at all. I
   confirmed this by reading both functions myself and then by running a before/after
   notification count, rather than trusting the explanation on its own.

3. **A place AI was incomplete and I had to course-correct (Issue #3).** Before reading
   the code closely, I asked the AI whether the `outerjoin(song_tags)` in
   `search_service` would cause duplicate rows for multi-tag songs. It answered "yes,
   an outer join fans out one row per tag, so a 3-tag song appears 3 times" — which is
   true of the SQL but wrong about the observable result here. When I actually ran
   `search_songs()` I got no duplicates. I had to correct the AI's answer myself: the
   service uses the legacy `db.session.query(Song)` interface, and legacy SQLAlchemy
   `Query.all()` de-duplicates full-entity rows by identity, so the fan-out is collapsed
   before the dicts are built. This is why I could not reproduce Issue #3 and pivoted to
   Issue #4. The AI's plausible-but-wrong answer is exactly the failure mode the project
   warns about, and running the code is what caught it.
