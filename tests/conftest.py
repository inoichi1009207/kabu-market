import socket

import pytest

from kabu_market import FakeClock, Store


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Tests must not access the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def store():
    db = Store()
    yield db
    db.close()
