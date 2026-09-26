"""Local background poller: one sync cycle about every interval, never blocking the API.

The cycle runs in a worker thread; a single-flight lock prevents overlapping cycles, and
"Sync Now" simply wakes the loop early. Replaceable by a hosted scheduler or eBay webhooks
that call orchestrator.run_cycle() directly.
"""
import asyncio
import logging
import threading
from datetime import datetime, timezone

LOG = logging.getLogger(__name__)


class SyncLoop:
    def __init__(self, cycle, interval_seconds):
        self.cycle = cycle
        self.interval = interval_seconds
        self.lock = threading.Lock()
        self.wake = asyncio.Event()
        self.task = None
        self.event_loop = None
        self.running = False
        self.last_started = None
        self.last_finished = None
        self.last_result = None

    def start(self):
        self.event_loop = asyncio.get_running_loop()
        self.task = asyncio.create_task(self._run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    def trigger(self):
        """Safe to call from any thread (API routes run in a threadpool)."""
        if self.event_loop is not None:
            self.event_loop.call_soon_threadsafe(self.wake.set)
        else:
            self.wake.set()

    async def _run(self):
        while True:
            await self.run_once()
            try:
                await asyncio.wait_for(self.wake.wait(), self.interval)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    async def run_once(self):
        if not self.lock.acquire(blocking=False):
            return None  # A cycle is already running.
        self.running = True
        self.last_started = datetime.now(timezone.utc)
        try:
            self.last_result = await asyncio.to_thread(self.cycle)
        except Exception as exc:
            LOG.exception('Sync cycle crashed')
            self.last_result = {'error': str(exc)[:500]}
        finally:
            self.last_finished = datetime.now(timezone.utc)
            self.running = False
            self.lock.release()
        return self.last_result

    def status(self):
        iso = lambda d: d.isoformat(timespec='seconds') if d else None
        return {'running': self.running, 'interval_seconds': self.interval, 'last_started': iso(self.last_started),
                'last_finished': iso(self.last_finished), 'last_result': self.last_result}
