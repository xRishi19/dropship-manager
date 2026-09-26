"""Sync status and the manual "Sync Now" trigger."""
import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Depends, Request

from .deps import get_db

router = APIRouter(tags=['sync'])


@router.get('/sync/status')
def status(request: Request, db=Depends(get_db)):
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
    loop = request.app.state.loop
    if loop.task is None:  # Background sync disabled in config: run one cycle on request.
        asyncio.create_task(loop.run_once())
    else:
        loop.trigger()
    return status(request, db)
