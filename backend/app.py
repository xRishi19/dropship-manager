"""FastAPI application. Run from the repository root:

    .venv/bin/uvicorn backend.app:create_app --factory --port 8000

(No --reload: a reloader process would start a second sync loop.)
On startup: load .env and config, apply pending migrations (with a DB backup), and start the
~60 s eBay/Gmail sync loop in the background. Nothing here calls OpenAI on its own; the
weekly sourcing run is only started by an explicit POST from the UI.
"""
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI

from .api import expenses, jobs, orders, overview, sourcing, sync
from .config import load_config
from .db.connection import connect
from .services.notifications.discord import SaleNotifier
from .services.sync.loop import SyncLoop
from .services.sync.orchestrator import SyncContext, run_cycle


def create_app(config_path=None, env_path=None, start_loop=None, context=None):
    """Build the FastAPI application on demand, load environment/configuration, migrate the database, and wire routes and background services. Importing the module has no side effects.

    Args:
        config_path: Optional configuration filename; defaults to APP_CONFIG or config.json.
        env_path: Optional dotenv filename; defaults to APP_ENV_FILE or .env.
        start_loop: Optional override for automatic sync; None uses config.sync.enabled.
        context: Optional existing SyncContext; otherwise one is created from configuration.

    Returns:
        Configured FastAPI instance. Background services start when its lifespan begins, not during factory execution.
    """
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    # httpx's INFO request logs include full URLs, including webhook credentials.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    load_dotenv(env_path or os.getenv('APP_ENV_FILE', '.env'))
    config = load_config(config_path or os.getenv('APP_CONFIG', 'config.json'))
    connect(config.database).close()  # Apply pending migrations once, before serving.
    context = context or SyncContext(config)
    notifications = SaleNotifier(config, os.getenv('DISCORD_SALES_WEBHOOK_URL'))
    context.notifications = notifications
    loop = SyncLoop(lambda: run_cycle(context), config.sync.interval_seconds)
    start_loop = config.sync.enabled if start_loop is None else start_loop

    @asynccontextmanager
    async def lifespan(app):
        """Start the sale-notification worker and optional sync loop for the application lifetime, then await their shutdown.

        Args:
            app: FastAPI application supplied by its lifespan protocol; the service instances are captured from the factory.

        Yields:
            Async generator yielding None once while the application serves requests; shutdown awaits both services.
        """
        notifications.start()
        if start_loop:
            loop.start()
        yield
        await loop.stop()
        await notifications.stop()

    app = FastAPI(title='Dropshipping Manager', lifespan=lifespan)
    app.state.notifications = notifications
    app.state.config = config
    app.state.context = context
    app.state.loop = loop
    app.state.jobs = jobs.JobRegistry()
    app.state.candidate_cache = {}
    for router in (overview.router, orders.router, expenses.router, sync.router, sourcing.router, jobs.router):
        app.include_router(router, prefix='/api')
    return app

