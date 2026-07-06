"""
tests/test_feed.py — Mixtape

Regression tests for the "Friends Listening Now" feed.

These target Issue #2 ("Friends Listening Now shows people from yesterday"). Against
the buggy code — where RECENT_THRESHOLD was 24 hours — test_listening_now_excludes_old
_events would fail, because a friend who last listened 2 hours ago was still shown as
"listening now."
"""

import pytest
from datetime import datetime, timedelta, timezone
from app import create_app, db
from models import User, Song, ListeningEvent, friendships
from services.feed_service import get_friends_listening_now


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


def _make_friendship(u1, u2):
    db.session.execute(friendships.insert().values(user_id=u1.id, friend_id=u2.id))
    db.session.execute(friendships.insert().values(user_id=u2.id, friend_id=u1.id))


@pytest.fixture
def seed(app):
    with app.app_context():
        me = User(username="me", email="me@example.com")
        recent_friend = User(username="recent", email="recent@example.com")
        stale_friend = User(username="stale", email="stale@example.com")
        db.session.add_all([me, recent_friend, stale_friend])
        db.session.flush()

        _make_friendship(me, recent_friend)
        _make_friendship(me, stale_friend)

        song = Song(title="Frequencies", artist="Static Era", shared_by=me.id)
        db.session.add(song)
        db.session.flush()

        now = datetime.now(timezone.utc)
        # recent_friend listened 5 minutes ago -> should appear
        db.session.add(ListeningEvent(
            user_id=recent_friend.id, song_id=song.id,
            listened_at=now - timedelta(minutes=5),
        ))
        # stale_friend listened 2 hours ago (yesterday-ish, still < 24h) -> should NOT appear
        db.session.add(ListeningEvent(
            user_id=stale_friend.id, song_id=song.id,
            listened_at=now - timedelta(hours=2),
        ))
        db.session.commit()
        yield {"me": me}


def test_listening_now_includes_recent_events(app, seed):
    """A friend who listened a few minutes ago appears in the feed."""
    with app.app_context():
        feed = get_friends_listening_now(seed["me"].id)
        usernames = [row["friend"]["username"] for row in feed]
        assert "recent" in usernames


def test_listening_now_excludes_old_events(app, seed):
    """
    A friend who last listened 2 hours ago must NOT appear in 'listening now'.
    This failed against the 24-hour threshold.
    """
    with app.app_context():
        feed = get_friends_listening_now(seed["me"].id)
        usernames = [row["friend"]["username"] for row in feed]
        assert "stale" not in usernames
