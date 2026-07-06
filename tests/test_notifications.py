"""
tests/test_notifications.py — Mixtape

Regression tests for notification behavior.

These target Issue #4 ("notified when a friend adds my song to a playlist, but not
when they rate it"). Against the buggy code — where rate_song() persisted the rating
but never called create_notification() — test_rating_others_song_notifies_sharer would
fail because zero notifications were created.
"""

import pytest
from app import create_app, db
from models import User, Song, Notification
from services.notification_service import rate_song


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def seed(app):
    """A sharer who owns a song, and a separate rater."""
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        rater = User(username="rater", email="rater@example.com")
        db.session.add_all([sharer, rater])
        db.session.flush()

        song = Song(title="After Hours", artist="Night City", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()
        yield {"sharer": sharer, "rater": rater, "song": song}


def test_rating_others_song_notifies_sharer(app, seed):
    """
    When a user rates someone else's song, the song's sharer receives exactly one
    'song_rated' notification. This is the behavior that was entirely missing before
    the fix.
    """
    with app.app_context():
        sharer_id = seed["sharer"].id
        rater_id = seed["rater"].id
        song_id = seed["song"].id

        rate_song(rater_id, song_id, 5)

        notifs = Notification.query.filter_by(
            user_id=sharer_id, notification_type="song_rated"
        ).all()
        assert len(notifs) == 1
        # The message should identify who rated it (not the sharer themselves).
        assert "rater" in notifs[0].body


def test_rating_own_song_does_not_notify(app, seed):
    """
    A user rating their own song must NOT generate a self-notification. This guards
    the `song.shared_by != user_id` condition in the fix.
    """
    with app.app_context():
        sharer_id = seed["sharer"].id
        song_id = seed["song"].id

        rate_song(sharer_id, song_id, 4)

        count = Notification.query.filter_by(
            user_id=sharer_id, notification_type="song_rated"
        ).count()
        assert count == 0
