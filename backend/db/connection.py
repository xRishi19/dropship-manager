"""SQLite connections and the migration runner.

Use one connection per thread: the API opens one per request, the sync loop its own.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .migrations import MIGRATIONS

KEEP_BACKUPS = 10


def connect(path: str, migrate: bool = True) -> sqlite3.Connection:
    """Open a row-oriented SQLite connection, configure concurrency, and optionally migrate its schema.

    Args:
        path: SQLite filename or :memory:; parent directories are created for disk files.
        migrate: Whether to apply outstanding schema migrations before returning.

    Returns:
        Open sqlite3.Connection owned and closed by the caller.
    """
    memory = path == ':memory:'
    if not memory:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # Each connection serves one request/cycle at a time, but FastAPI may open it in one
    # worker thread and use it in another.
    db = sqlite3.connect(path, timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=30000')
    if not memory:
        # Readers (API) and the writer (sync loop) must not block each other.
        db.execute('PRAGMA journal_mode=WAL')
    if migrate:
        apply_migrations(db, None if memory else Path(path))
    return db


def apply_migrations(db, path=None):
    """Apply pending numbered migrations and record each successful version. A non-empty file database is backed up first.

    Args:
        db: Open SQLite connection on which migrations and their version records run.
        path: Optional database Path used for backups; omit for an in-memory database.

    Returns:
        Path to the pre-migration backup, or None if no backup was required. Migration transactions are managed here.
    """
    db.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, '
               'name TEXT NOT NULL, applied_at TEXT NOT NULL)')
    applied = {row[0] for row in db.execute('SELECT version FROM schema_migrations')}
    pending = [m for m in MIGRATIONS if m[0] not in applied]
    if not pending:
        return None
    backup = None
    existing = db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name!='schema_migrations'").fetchone()[0]
    if path is not None and existing:
        backup = backup_database(db, path)
    for version, name, migration in pending:
        with db:
            migration(db)
            db.execute('INSERT INTO schema_migrations(version, name, applied_at) VALUES (?,?,?)',
                       (version, name, datetime.now(timezone.utc).isoformat(timespec='seconds')))
    return backup


def backup_database(db, path):
    """Copy a consistent SQLite snapshot into the sibling backups directory and prune older snapshots beyond KEEP_BACKUPS.

    Args:
        db: Open source SQLite connection to snapshot using SQLite backup.
        path: Source database Path used to derive backup directory and filename.

    Returns:
        Path to the new timestamped backup; the temporary destination connection is closed here.
    """
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target = path.parent / 'backups' / f'{path.stem}-{stamp}.sqlite3'
    target.parent.mkdir(parents=True, exist_ok=True)
    destination = sqlite3.connect(target)
    try:
        db.backup(destination)
    finally:
        destination.close()
    for old in sorted(target.parent.glob(f'{path.stem}-*.sqlite3'))[:-KEEP_BACKUPS]:
        old.unlink()
    return target
