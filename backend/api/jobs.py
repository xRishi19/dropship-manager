"""In-process background jobs (single local process). Used for the explicit weekly run."""
import threading
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(tags=['jobs'])


class JobRegistry:
    def __init__(self):
        self.jobs = {}
        self.lock = threading.Lock()

    def running(self, kind):
        return next((j for j in self.jobs.values() if j['kind'] == kind and j['status'] == 'running'), None)

    def start(self, kind, target):
        """Run target(job) in a thread; at most one running job per kind."""
        with self.lock:
            if self.running(kind):
                raise HTTPException(409, f'A {kind} job is already running')
            job = {'id': uuid.uuid4().hex, 'kind': kind, 'status': 'running', 'step': 'starting',
                   'started_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                   'finished_at': None, 'error': None, 'result': None}
            self.jobs[job['id']] = job

        def run():
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
    """The most recent job of a kind, so the UI can resume showing progress after navigation."""
    jobs = [j for j in request.app.state.jobs.jobs.values() if j['kind'] == kind]
    return max(jobs, key=lambda j: j['started_at']) if jobs else None


@router.get('/jobs/{job_id}')
def get_job(job_id: str, request: Request):
    job = request.app.state.jobs.jobs.get(job_id)
    if not job:
        raise HTTPException(404, 'Job not found')
    return job
