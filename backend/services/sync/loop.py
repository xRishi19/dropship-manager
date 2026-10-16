"""Local background poller: one sync cycle about every interval, never blocking the API.

The cycle runs in a worker thread; a single-flight lock prevents overlapping cycles, and
Manual sync requests reserve the same lock before scheduling work. Replaceable by a hosted scheduler or eBay webhooks
that call orchestrator.run_cycle() directly.
"""
import asyncio
import logging
import threading
from datetime import datetime, timezone

LOG = logging.getLogger(__name__)


class SyncLoop:
    def __init__(self, cycle, interval_seconds):
        """Initialize the local synchronization scheduler and shared single-flight execution lock.

        Args:
            cycle: Zero-argument callable that runs a complete synchronization cycle.
            interval_seconds: Polling delay in seconds between synchronization attempts.

        Returns:
            None; prepares task tracking, wake events and execution status.
        """
        self.cycle = cycle
        self.interval = interval_seconds
        self.lock = threading.Lock()
        self.wake = asyncio.Event()
        self.task = None
        self.event_loop = None
        self.manual_tasks = set()
        self.running = False
        self.last_started = None
        self.last_finished = None
        self.last_result = None

    def start(self):
        """Attach the scheduler to the running event loop and start polling immediately.

        Args:
            None.

        Returns:
            None; stores the created background task.
        """
        self.event_loop = asyncio.get_running_loop()
        self.task = asyncio.create_task(self._run())

    async def stop(self):
        """Wait for manually submitted work and cancel the poller while allowing in-flight work to finish.

        Args:
            None.

        Returns:
            None after tracked synchronization work has stopped.
        """
        if self.manual_tasks:
            await asyncio.gather(*self.manual_tasks)
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    def trigger(self):
        """Wake the poller safely from either the API threadpool or the scheduler loop.

        Safe to call from any thread (API routes run in a threadpool).

        Args:
            None.

        Returns:
            None; schedules or sets the wake event.
        """
        if self.event_loop is not None:
            self.event_loop.call_soon_threadsafe(self.wake.set)
        else:
            self.wake.set()

    async def _run(self):
        """Repeatedly run one cycle, waiting for the configured interval or an explicit wake request.

        Args:
            None.

        Returns:
            None if cancelled; otherwise runs indefinitely.
        """
        while True:
            await self.run_once()
            try:
                await asyncio.wait_for(self.wake.wait(), self.interval)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    def submit(self, operation=None):
        """Reserve the shared lock synchronously and schedule manual synchronization work.

        Reserve synchronously so concurrent HTTP requests cannot both be accepted.

        Args:
            operation: Callable to run under the synchronization lock; None uses the default cycle.

        Returns:
            True when accepted, False when another sync already owns the lock.
        """
        if not self.lock.acquire(blocking=False):
            return False
        self.running = True
        task = asyncio.create_task(self._execute(operation or self.cycle))
        self.manual_tasks.add(task)
        task.add_done_callback(self.manual_tasks.discard)
        return True

    async def run_once(self):
        """Try to run the default cycle under the shared execution lock.

        Args:
            None.

        Returns:
            Cycle result, or None when another execution is in progress.
        """
        if not self.lock.acquire(blocking=False):
            return None  # A cycle is already running.
        return await self._execute(self.cycle)

    async def _execute(self, operation):
        """Run reserved sync work in a worker thread and retain status through cancellation/shutdown.

        Args:
            operation: Zero-argument callable for the already reserved sync execution; the caller must supply it.

        Returns:
            Operation result, or an error dictionary if the operation raises; always releases the lock.
        """
        self.running = True
        self.last_started = datetime.now(timezone.utc)
        try:
            job = asyncio.create_task(asyncio.to_thread(operation))
            try:
                self.last_result = await asyncio.shield(job)
            except asyncio.CancelledError:
                self.last_result = await job  # Hold the lock and retain results until the worker finishes.
                raise
        except Exception as exc:
            LOG.exception('Sync cycle crashed')
            self.last_result = {'error': str(exc)[:500]}
        finally:
            self.last_finished = datetime.now(timezone.utc)
            self.running = False
            self.lock.release()
        return self.last_result

    def status(self):
        """Serialize current scheduler progress and most recent execution timestamps/results.

        Args:
            None.

        Returns:
            Dictionary with running, interval_seconds, last_started, last_finished and last_result.
        """
        iso = lambda d: d.isoformat(timespec='microseconds') if d else None
        return {'running': self.running, 'interval_seconds': self.interval, 'last_started': iso(self.last_started),
                'last_finished': iso(self.last_finished), 'last_result': self.last_result}
