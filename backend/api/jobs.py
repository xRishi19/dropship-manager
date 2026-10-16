"""In-process background jobs (single local process). Used for the explicit weekly run."""
import threading
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(tags=['jobs'])


class JobRegistry:
    def __init__(self):
        """Initialize the in-process job registry and synchronization lock.

        Args:
            None.

        Returns:
            None; starts with an empty jobs mapping.
        """
        self.jobs = {}
        self.lock = threading.Lock()

    def running(self, kind):
        """Find an active job of the requested kind in the registry.

        Args:
            kind: Job category used for lookup and same-kind concurrency control.

        Returns:
            Job dictionary, or None when no matching job is running.
        """
        return next((j for j in self.jobs.values() if j['kind'] == kind and j['status'] == 'running'), None)

    def start(self, kind, target):
        """Register and launch a daemon-thread job while preventing overlapping jobs of the same kind.

        Run target(job) in a thread; at most one running job per kind.

        Args:
            kind: Job category used for lookup and same-kind concurrency control.
            target: Callable accepting the mutable job dictionary and returning its final result.

        Returns:
            Mutable job dictionary used for polling; concurrent same-kind requests raise HTTPException 409.
        """
        with self.lock:
            if self.running(kind):
                raise HTTPException(409, f'A {kind} job is already running')
            job = {'id': uuid.uuid4().hex, 'kind': kind, 'status': 'running', 'step': 'starting',
                   'started_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                   'finished_at': None, 'error': None, 'result': None}
            self.jobs[job['id']] = job

        def run():
            """Execute the enclosing job target and persist its result, sanitized-length error and completion time.

            Args:
                None.

            Returns:
                None; updates the registered job dictionary in place.
            """
            try:
                job['result'] = target(job)
                job['status'] = 'succeeded'
            except Exception as exc:
                job['status'], job['error'] = 'failed', str(exc)[:800]
            finally:
                job['finished_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')

        threading.Thread(target=run, name=f'job-{kind}', daemon=True).start()
        return job


@router.get('/jobs/latest')
def latest_job(kind: str, request: Request):
    """Find the newest registered job of a kind so the frontend can resume its progress display.

    The most recent job of a kind, so the UI can resume showing progress after navigation.

    Args:
        kind: Job category used for lookup and same-kind concurrency control.
        request: FastAPI request providing application configuration and shared runtime services.

    Returns:
        Most recently started job dictionary, or None when none exists.
    """
    jobs = [j for j in request.app.state.jobs.jobs.values() if j['kind'] == kind]
    return max(jobs, key=lambda j: j['started_at']) if jobs else None


@router.get('/jobs/{job_id}')
def get_job(job_id: str, request: Request):
    """Look up a registered background job for progress polling.

    Args:
        job_id: Registry identifier returned when the background job was started.
        request: FastAPI request providing application configuration and shared runtime services.

    Returns:
        Job dictionary; unknown identifiers raise HTTPException 404.
    """
    job = request.app.state.jobs.jobs.get(job_id)
    if not job:
        raise HTTPException(404, 'Job not found')
    return job
