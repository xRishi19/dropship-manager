import pytest
from backend.db.connection import connect
from backend.config import Config


@pytest.fixture
def db():
    connection = connect(':memory:')
    yield connection
    connection.close()


@pytest.fixture
def config(tmp_path):
    return Config(output_dir=str(tmp_path / 'output'))


@pytest.fixture(autouse=True)
def no_gmail_pacing(monkeypatch):
    """The sync loop paces Gmail fetches to stay under Google's per-minute quota; tests don't need to wait."""
    monkeypatch.setattr('backend.services.sync.orchestrator.GMAIL_FETCH_PAUSE_SECONDS', 0)
