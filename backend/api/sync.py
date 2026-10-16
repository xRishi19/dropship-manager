"""Sync status and the manual "Sync Now" trigger."""
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from ..services.sync.orchestrator import STEPS, run_cycle
from ..services.notifications.discord import WebhookTestError

from .deps import get_db

router = APIRouter(tags=['sync'])


@router.post('/sync/webhook/test')
def test_webhook(request: Request):
    """Send a clearly labeled synthetic Discord message and translate sanitized delivery failures to HTTP errors.

    Args:
        request: FastAPI request providing application configuration and shared runtime services.

    Returns:
        Success/message dictionary; leaves sale notification records unchanged.
    """
    try:
        return request.app.state.notifications.send_test()
    except WebhookTestError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None


@router.get('/sync/status')
def status(request: Request, db=Depends(get_db)):
    """Combine saved step progress with scheduler state and non-secret integration readiness.

    Args:
        request: FastAPI request providing application configuration and shared runtime services.
        db: Open SQLite connection used for the query or write.

    Returns:
        Dictionary containing loop, steps, gmail_connected and accounting_start.
    """
    steps = {}
    for row in db.execute('SELECT * FROM sync_state ORDER BY name'):
        steps[row['name']] = {**dict(row), 'details': json.loads(row['details_json']) if row['details_json'] else None}
        steps[row['name']].pop('details_json')
    config = request.app.state.config
    loop = request.app.state.loop
    return {'loop': {**loop.status(), 'enabled': loop.task is not None,
                     'current_step': getattr(request.app.state.context, 'current_step', None)},
            'steps': steps,
            'gmail_connected': Path(config.gmail.token_path).exists(),
            'accounting_start': config.accounting.start_date}


@router.post('/sync/now')
async def sync_now(request: Request, db=Depends(get_db)):
    """Reserve and schedule a complete synchronization cycle through the shared execution lock.

    Args:
        request: FastAPI request providing application configuration and shared runtime services.
        db: Open SQLite connection used for the query or write.

    Returns:
        Current sync-status dictionary; returns HTTPException 409 if synchronization is already running.
    """
    if not request.app.state.loop.submit():
        raise HTTPException(409, 'A sync is already running')
    return status(request, db)


@router.post('/sync/steps/{name}')
async def sync_step(name: str, request: Request, db=Depends(get_db)):
    """Validate a step name and schedule only that step with manually forced interval behavior.

    Args:
        name: One of the nine registered synchronization step identifiers.
        request: FastAPI request providing application configuration and shared runtime services.
        db: Open SQLite connection used for the query or write.

    Returns:
        Current sync-status dictionary; unknown names raise 404 and busy synchronization raises 409.
    """
    if name not in dict(STEPS):
        raise HTTPException(404, 'Unknown sync step')
    context = request.app.state.context
    if not request.app.state.loop.submit(lambda: run_cycle(context, step_name=name)):
        raise HTTPException(409, 'A sync is already running')
    return status(request, db)
