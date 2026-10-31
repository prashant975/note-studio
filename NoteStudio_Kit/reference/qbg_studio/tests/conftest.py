"""Unit tests must never spend API credits or edit live reports."""
import pytest
import requests


@pytest.fixture(autouse=True)
def block_unmocked_network(monkeypatch):
    def refused(*args, **kwargs):
        raise RuntimeError('External network is disabled in unit tests; mock this request.')
    monkeypatch.setattr(requests.sessions.Session, 'request', refused)
