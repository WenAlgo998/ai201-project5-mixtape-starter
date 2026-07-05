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
