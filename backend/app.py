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
from .services.sync.loop import SyncLoop
from .services.sync.orchestrator import SyncContext, run_cycle


def create_app(config_path=None, env_path=None, start_loop=None, context=None):
    """Built on demand (uvicorn --factory), so importing this module has no side effects."""
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    load_dotenv(env_path or os.getenv('APP_ENV_FILE', '.env'))
    config = load_config(config_path or os.getenv('APP_CONFIG', 'config.json'))
    connect(config.database).close()  # Apply pending migrations once, before serving.
    context = context or SyncContext(config)
    loop = SyncLoop(lambda: run_cycle(context), config.sync.interval_seconds)
    start_loop = config.sync.enabled if start_loop is None else start_loop

    @asynccontextmanager
    async def lifespan(app):
        if start_loop:
            loop.start()
        yield
        await loop.stop()

    app = FastAPI(title='Dropshipping Manager', lifespan=lifespan)
    app.state.config = config
    app.state.context = context
    app.state.loop = loop
    app.state.jobs = jobs.JobRegistry()
    app.state.candidate_cache = {}
    for router in (overview.router, orders.router, expenses.router, sync.router, sourcing.router, jobs.router):
        app.include_router(router, prefix='/api')
    return app

