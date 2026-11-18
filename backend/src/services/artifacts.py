import os
from contextlib import contextmanager
from pathlib import Path
ARTIFACTS=Path(os.environ.get('ARTIFACTS_DIR','/app/artifacts'))
LOCK_NAME='.purge.lock'
def run_file(run_id): return ARTIFACTS/f'run-{run_id}.json'
@contextmanager
def lock():
    """Serialize artifact writes and purge deletions across worker processes."""
    ARTIFACTS.mkdir(parents=True,exist_ok=True)
    handle=open(ARTIFACTS/LOCK_NAME,'a+')
    try:
        import fcntl
        fcntl.flock(handle,fcntl.LOCK_EX)
        yield
    finally:
        try:
            import fcntl
            fcntl.flock(handle,fcntl.LOCK_UN)
        finally:
            handle.close()
def files_for(run_id):
    return [p for p in ARTIFACTS.glob(f'run-{run_id}.*') if p.is_file()]
