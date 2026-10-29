"""Durable independent gVisor execution service.

Guest files never get extracted on the runner host. Completed snapshots are
immutable, and a lost execution receipt never causes automatic command replay.
"""
import base64
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .protocol import Command, MAX_FILE, MAX_WORKSPACE, TERMINAL, digest, file_path, pack, unpack


class RunnerBusy(ValueError):
    pass


class Engine:
    def __init__(self, root, backend):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.backend = backend
        self.mutex = threading.RLock()
        self.running = set()
        with self.db() as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE IF NOT EXISTS executions (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, body TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS workspaces (id TEXT PRIMARY KEY, execution TEXT NOT NULL)')

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.root / 'jobs.sqlite', timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, eid, wid=None):
        with self.db() as connection:
            row = connection.execute('SELECT body FROM executions WHERE id=?', (eid,)).fetchone()
        if not row:
            raise KeyError(eid)
        value = json.loads(row['body'])
        if wid is not None and value['workspace_id'] != wid:
            raise KeyError(eid)
        return value

    def save(self, value):
        with self.db() as connection:
            connection.execute('UPDATE executions SET body=? WHERE id=?', (json.dumps(value), value['execution_id']))
        return value

    def submit(self, command):
        value = command.model_dump()
        with self.mutex, self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT body FROM executions WHERE id=?', (command.execution_id,)).fetchone()
            if row:
                saved = json.loads(row['body'])
                if saved['request_sha256'] != command.sha256() or saved['workspace_id'] != command.workspace_id:
                    raise ValueError('Idempotency key was already used for a different request')
                return self.public(saved)
            rows = connection.execute("SELECT workspace,json_extract(body,'$.state') AS state FROM executions").fetchall()
            active = [row for row in rows if row['state'] not in TERMINAL]
            if len(active) >= 16:
                raise RunnerBusy('Runner queue is full')
            used = sum(path.stat().st_size for path in self.root.iterdir() if path.is_file())
            if used + MAX_WORKSPACE + 3 * 1024 * 1024 > int(os.getenv('ATLAS_RUNNER_STORAGE_MB', '4096')) * 1024 * 1024:
                raise ValueError('Runner checkpoint storage quota reached; operator maintenance is required')
            value.update(state='queued', request_sha256=command.sha256(), created_at=time.time(),
                         cancel_requested=False, snapshot_complete=False, stdout='', stderr='')
            connection.execute('INSERT INTO executions VALUES (?,?,?)', (command.execution_id, command.workspace_id, json.dumps(value)))
        return self.public(value)

    def cancel(self, eid, wid):
        with self.mutex:
            value = self.get(eid, wid)
            if value['state'] not in TERMINAL:
                value['cancel_requested'] = True
                self.save(value)
            return self.public(value)

    def latest(self, wid):
        with self.db() as connection:
            row = connection.execute('SELECT execution FROM workspaces WHERE id=?', (wid,)).fetchone()
        return self.get(row['execution'], wid) if row else None

    def snapshot(self, value):
        if not value.get('snapshot_complete'):
            return {}
        raw = (self.root / (value['execution_id'] + '.tar')).read_bytes()
        if digest(raw) != value['snapshot_sha256']:
            raise ValueError('Workspace checkpoint integrity check failed')
        return unpack(raw)

    def file(self, eid, wid, name):
        value = self.get(eid, wid)
        files = self.snapshot(value)
        content = files[file_path(name)]
        return {'execution_id': eid, 'workspace_id': wid, 'path': name, 'sha256': digest(content),
                'bytes': len(content), 'content_base64': base64.b64encode(content).decode()}

    @staticmethod
    def public(value):
        return {key: item for key, item in value.items() if key not in ('files', 'container_id', 'exec_id', 'command')}

    def step(self, eid):
        """Advance a persisted state machine. No command is launched twice."""
        with self.mutex:
            value = self.get(eid)
            if value['state'] in TERMINAL:
                return
            if value['cancel_requested']:
                if value.get('container_id'):
                    self.backend.remove(value['container_id'])
                value.update(state='cancelled', finished_at=time.time())
                self.save(value)
                return
            if value['state'] == 'queued':
                with self.db() as connection:
                    rows = connection.execute("SELECT workspace,json_extract(body,'$.state') AS state FROM executions").fetchall()
                if sum(row['state'] in ('preparing', 'starting', 'running') for row in rows) >= 4:
                    return
                if any(row['workspace']==value['workspace_id'] and row['state'] in ('preparing','starting','running') for row in rows):
                    return
                value.update(state='preparing', started_at=time.time())
                self.save(value)
                try:
                    previous = self.latest(value['workspace_id'])
                    files = self.snapshot(previous) if previous else {}
                    files.update({entry['path']: base64.b64decode(entry['content_base64']) for entry in value['files']})
                    if len(files) > 1000 or sum(map(len, files.values())) > MAX_WORKSPACE:
                        raise ValueError('Workspace input exceeds checkpoint budget')
                    container = self.backend.create(value)
                    value['container_id'] = container
                    self.save(value)
                    self.backend.start(container)
                    self.backend.stage(container, pack(files))
                    execution = self.backend.prepare_exec(container, value['command'], value['timeout_seconds'])
                    # Store the exec ID before asking Docker to start it. A
                    # restart in this window is unknown, never a second start.
                    value.update(exec_id=execution, state='starting')
                    self.save(value)
                    self.backend.start_exec(execution)
                    value['state'] = 'running'
                    value['files']=[];value['command']=''
                    self.save(value)
                except Exception:
                    self.backend.remove(value.get('container_id') or 'atlas-work-' + eid)
                    value.update(state='unknown' if value.get('exec_id') else 'failed',
                                 error='Sandbox preparation or start did not complete; the command was not replayed', finished_at=time.time())
                    self.save(value)
                return
            if value['state'] in ('preparing', 'starting'):
                # A control-plane restart during an unacknowledged effect is
                # not proof that it is safe to repeat the effect.
                self.backend.remove(value.get('container_id') or 'atlas-work-' + eid)
                value.update(state='unknown', error='Runner restarted during command startup; command not replayed', finished_at=time.time())
                self.save(value)
                return
            execution = self.backend.inspect_exec(value['exec_id'])
            if execution['Running'] and time.time() > value['started_at'] + value['timeout_seconds'] + 15:
                self.backend.remove(value['container_id'])
                value.update(state='timed_out',exit_code=124,finished_at=time.time())
                self.save(value)
                return
            try:
                value.update(self.backend.logs(value['container_id']))
            except Exception:
                value['log_notice']='日志暂不可读；执行状态与超时仍独立检查'
            if execution['Running']:
                self.save(value)
                return
            code = execution.get('ExitCode')
            if type(code) is not int:
                raise ValueError('Docker did not return a command exit code')
            try:
                self.backend.quiesce(value['container_id'])
                files = self.backend.files(value['container_id'])
                raw = pack(files)
                path = self.root / (eid + '.tar')
                temporary = path.with_suffix('.tmp')
                try:
                    with temporary.open('wb') as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
                value.update(snapshot_complete=True, snapshot_sha256=digest(raw),
                             output_files=[{'path': name, 'bytes': len(content), 'sha256': digest(content)} for name, content in sorted(files.items())])
            except Exception:
                value.update(snapshot_complete=False, error='Output checkpoint rejected; the previous checkpoint is preserved')
            finally:
                self.backend.remove(value['container_id'])
            value.update(state='timed_out' if code == 124 else 'succeeded' if code == 0 and value['snapshot_complete'] else 'failed',
                         exit_code=code, finished_at=time.time())
            # Publish the immutable snapshot receipt and latest pointer in one
            # transaction. A crash must never replace a valid pointer with an
            # execution whose checkpoint metadata has not been committed.
            with self.db() as connection:
                connection.execute('UPDATE executions SET body=? WHERE id=?', (json.dumps(value), eid))
                if value['snapshot_complete']:
                    connection.execute('INSERT INTO workspaces VALUES (?,?) ON CONFLICT(id) DO UPDATE SET execution=excluded.execution', (value['workspace_id'], eid))

    def sweep(self):
        with self.db() as connection:
            rows = connection.execute("SELECT id,json_extract(body,'$.state') AS state FROM executions ORDER BY rowid").fetchall()
        for row in rows:
            if row['state'] in TERMINAL:
                continue
            try:
                self.step(row['id'])
            except Exception as error:
                # Keep known running IDs for recovery on a later sweep. The
                # container's fixed maximum lifetime also bounds orphan work.
                import httpx
                if isinstance(error,httpx.HTTPStatusError) and error.response.status_code==404:
                    value=self.get(row['id'])
                    value.update(state='unknown',error='Docker execution state disappeared; command not replayed',finished_at=time.time())
                    self.save(value)


def create_app(engine=None, token=None):
    secret = token if token is not None else os.getenv('ATLAS_WORK_RUNNER_TOKEN', '')
    if len(secret) < 32:
        raise ValueError('Runner needs a dedicated token of at least 32 characters')
    if engine is None:
        from .docker_backend import DockerBackend
        engine = Engine(os.getenv('ATLAS_RUNNER_DATA', '/state'), DockerBackend())

    @asynccontextmanager
    async def lifespan(app):
        stop = threading.Event()
        def run():
            while not stop.is_set():
                engine.sweep()
                stop.wait(.5)
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            yield
        finally:
            stop.set()
            worker.join(timeout=2)

    app = FastAPI(title='Atlas independent work runner', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware('http')
    async def boundary(request, call_next):
        supplied = request.headers.get('Authorization', '').removeprefix('Bearer ')
        if not secrets.compare_digest(supplied.encode(), secret.encode()):
            return JSONResponse(status_code=401, content={'detail': 'Runner authentication required'})
        if request.method == 'POST':
            size = request.headers.get('content-length')
            if not size or not size.isdigit():
                return JSONResponse(status_code=411, content={'detail': 'Content-Length required'})
            if int(size) > 92 * 1024 * 1024:
                return JSONResponse(status_code=413, content={'detail': 'Request exceeds input budget'})
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        return response

    from fastapi.exceptions import RequestValidationError
    @app.exception_handler(RequestValidationError)
    def invalid_request(request, error):
        return JSONResponse(status_code=422, content={'detail': 'Invalid execution request; input was not executed'})

    async def auth(request: Request):
        supplied = request.headers.get('Authorization', '').removeprefix('Bearer ')
        if not secrets.compare_digest(supplied.encode(), secret.encode()):
            raise HTTPException(401, 'Runner authentication required')
        size = request.headers.get('content-length')
        if size and (not size.isdigit() or int(size) > 92 * 1024 * 1024):
            raise HTTPException(413, 'Request exceeds input budget')

    @app.exception_handler(ValueError)
    def invalid(request, error):
        return JSONResponse(status_code=409, content={'detail': str(error)[:200]})

    @app.exception_handler(RunnerBusy)
    def busy(request,error):
        return JSONResponse(status_code=503,content={'detail':'Runner queue is full; retry the same execution ID'},headers={'Retry-After':'2'})

    @app.exception_handler(KeyError)
    def missing(request, error):
        return JSONResponse(status_code=404, content={'detail': 'Execution or file is unavailable'})

    @app.get('/v1/health', dependencies=[Depends(auth)])
    def health():
        try:
            return {'ready': True, 'protocol': 1, **engine.backend.ready()}
        except Exception:
            return JSONResponse(status_code=503, content={'ready': False, 'protocol': 1})

    @app.post('/v1/executions', dependencies=[Depends(auth)])
    def submit(value: Command):
        return engine.submit(value)

    def identifiers(eid, wid):
        if not re.fullmatch(r'[a-f0-9]{64}', eid) or not re.fullmatch(r'[a-f0-9]{64}', wid):
            raise HTTPException(404, 'Execution unavailable')

    @app.get('/v1/executions/{eid}', dependencies=[Depends(auth)])
    def read(eid: str, workspace_id: str):
        identifiers(eid, workspace_id)
        return engine.public(engine.get(eid, workspace_id))

    @app.post('/v1/executions/{eid}/cancel', dependencies=[Depends(auth)])
    def cancel(eid: str, workspace_id: str):
        identifiers(eid, workspace_id)
        return engine.cancel(eid, workspace_id)

    @app.get('/v1/executions/{eid}/file', dependencies=[Depends(auth)])
    def file(eid: str, workspace_id: str, path: str):
        identifiers(eid, workspace_id)
        return engine.file(eid, workspace_id, path)

    return app
