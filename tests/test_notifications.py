"""
tests/test_notifications.py — Mixtape

Tests for rate_song's notification behavior.
"""

import pytest
from app import create_app, db
from models import User, Song
from services.notification_service import rate_song, get_notifications


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def sharer_and_song(app):
    """Create a song sharer and a song they shared."""
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        db.session.add(sharer)
        db.session.flush()

        song = Song(title="Late Bloomer", artist="Some Band", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()

        yield {"sharer": sharer, "song": song}


def test_rate_song_notifies_sharer(app, sharer_and_song):
    """Rating someone else's shared song notifies the sharer."""
    with app.app_context():
        sharer = sharer_and_song["sharer"]
        song = sharer_and_song["song"]

        rater = User(username="rater", email="rater@example.com")
        db.session.add(rater)
        db.session.commit()

        assert get_notifications(sharer.id) == []

        rate_song(rater.id, song.id, 5)

        notifications = get_notifications(sharer.id)
        assert len(notifications) == 1
        assert notifications[0]["type"] == "song_rated"


def test_rate_song_self_rating_does_not_notify(app, sharer_and_song):
    """Rating your own shared song does not create a notification."""
    with app.app_context():
        sharer = sharer_and_song["sharer"]
        song = sharer_and_song["song"]

        rate_song(sharer.id, song.id, 4)

        assert get_notifications(sharer.id) == []
