import pytest
from backend.db.connection import connect
from backend.config import Config


@pytest.fixture
def db():
    """Open a migrated in-memory SQLite database and close it after the consuming test.

    Args:
        None.

    Yields:
        SQLite connection with the complete current schema; it is closed during fixture
        teardown.
    """
    connection = connect(':memory:')
    yield connection
    connection.close()


@pytest.fixture
def config(tmp_path):
    """Create validated application defaults with exports isolated in a temporary directory.

    Args:
        tmp_path: Pytest temporary directory for isolated databases, configuration files,
            exports, and backups.

    Returns:
        Config instance whose output_dir points inside tmp_path.
    """
    return Config(output_dir=str(tmp_path / 'output'))


@pytest.fixture(autouse=True)
def no_gmail_pacing(monkeypatch):
    """The sync loop paces Gmail fetches to stay under Google's per-minute quota; tests don't need to wait.

    Disable the Gmail fetch delay for the duration of each test so synthetic ingestion does not wait for production pacing.

    Args:
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.

    Returns:
        None. Pytest restores the pacing setting after the test.
    """
    monkeypatch.setattr('backend.services.sync.orchestrator.GMAIL_FETCH_PAUSE_SECONDS', 0)


@pytest.fixture(autouse=True)
def no_live_discord(monkeypatch):
    """App tests must never inherit a real notification destination from .env.

    Set the webhook environment variable to an empty value so tests cannot inherit the real local Discord destination.

    Args:
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.

    Returns:
        None. Individual tests may explicitly override this fixture with a synthetic webhook.
    """
    monkeypatch.setenv('DISCORD_SALES_WEBHOOK_URL', '')
