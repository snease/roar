"""Isolated, persistent ALIGN placement runs; no Qt, browser, or ALIGN imports."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

PROFILE = json.loads(Path(__file__).with_name('align_profile.json').read_text(encoding='utf-8'))
PROFILE_ID = PROFILE['id']
_ACTIVE = {'starting', 'generating', 'serving', 'stopping'}


def _write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def project_fingerprint(project):
    data = json.dumps(project.to_dict(), sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(data.encode()).hexdigest()


def validate_ota_project(project):
    if getattr(project, 'align_profile', None) != PROFILE_ID:
        raise ValueError(f'Select the supported OTA profile: {PROFILE_ID}')
    devices = project.devices
    if len(devices) != PROFILE['mos_count'] or any(
            d.kind != 'mos' or d.model.lower() not in PROFILE['models'] or len(d.terminals) != 4 for d in devices):
        raise ValueError('This ALIGN profile supports the ten-MOS SKY130 OTA only')


@dataclass(frozen=True)
class AlignConfig:
    python: str
    backend_path: str
    run_root: str
    port: int = 0

    @property
    def profile_id(self):
        return PROFILE_ID

    @classmethod
    def from_environment(cls):
        state = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local' / 'state'))
        return cls(os.environ.get(PROFILE['python_environment'], ''),
                   os.environ.get(PROFILE['backend_environment'], ''), str(state / 'roar' / 'align' / 'runs'))

    def validate(self):
        if sys.platform != 'linux':
            raise ValueError('The ALIGN launcher currently requires Linux or WSL; floorplan editing and export remain available')
        if not self.python or not Path(self.python).expanduser().is_file() or not os.access(Path(self.python).expanduser(), os.X_OK):
            raise ValueError('Select the Python executable in the separate ALIGN environment')
        if not self.backend_path:
            raise ValueError('Select the tested vibeic SKY130_PDK backend (ROAR_ALIGN_PDK_FIXED)')
        backend = Path(self.backend_path).expanduser()
        for name, expected in PROFILE['backend_hashes'].items():
            path = backend / name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f'Backend does not match {PROFILE["backend_repository"]}@{PROFILE["backend_revision"]}: {name}')
        if not self.run_root:
            raise ValueError('Select a directory for isolated ALIGN runs')
        if type(self.port) is not int or not 0 <= self.port <= 65535:
            raise ValueError('ALIGN port must be 0 (automatic) or an integer in 1..65535')


@dataclass(frozen=True)
class RunState:
    run_dir: str
    status: str
    url: str | None
    error: str | None
    log_path: str
    fingerprint: str
    stale: bool = False
    can_stop: bool = False
    has_placement: bool = False


def _alive(manifest, run_dir):
    """Verify PID ownership before reconnecting or signalling a persisted run."""
    active = manifest.get('active') or {}
    pid, token = active.get('pid'), active.get('token')
    if not isinstance(pid, int) or not token:
        return False
    try:
        proc = Path('/proc') / str(pid)
        arguments = (proc / 'cmdline').read_bytes().split(b'\0')
        state = (proc / 'stat').read_text().split(') ', 1)[1][0]
        return state != 'Z' and token.encode() in arguments and str(run_dir / 'run.json').encode() in arguments
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False


def _ready(url):
    parsed = urllib.parse.urlparse(url or '')
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port:
        return False
    try:
        # Bypass proxy environment variables for the worker's local server.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url.rstrip('/') + '/_dash-layout', timeout=0.35) as response:
            return response.status == 200 and isinstance(json.load(response), dict)
    except (OSError, ValueError, urllib.error.URLError):
        return False


class AlignRunner:
    """Own one run at a time. Call poll from an event loop; stop is nonblocking.

    The durable index is scoped to the saved floorplan path. An unsaved run can
    be adopted after Save As only when its captured fingerprint still matches.
    Existing named floorplans never borrow one another's run history.
    """
    def __init__(self, config, project_path=None):
        self.config = config
        self.project_path = str(Path(project_path).expanduser().resolve()) if project_path else None
        self._run_dir = None
        self._process = None
        self._verified_ready = None
        self._dump_cache = {}

    @property
    def _root(self):
        return Path(self.config.run_root).expanduser().resolve()

    def _index(self, key):
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self._root / 'last_runs' / (digest + '.json')

    def _has_dump(self, directory):
        path = Path(directory) / '3_pnr' / '__placer_dump__.json'
        try:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            cached = self._dump_cache.get(str(path))
            if cached and cached[0] == signature:
                return cached[1]
            dump = json.loads(path.read_text())
            valid = isinstance(dump, list) and len(dump) == 4 and bool(dump[2])
            self._dump_cache[str(path)] = (signature, valid)
            return valid
        except (OSError, ValueError):
            return False

    def _pointer(self, manifest):
        key = self.project_path or 'snapshot:' + manifest['fingerprint']
        path = self._index(key)
        return json.loads(path.read_text()) if path.is_file() else {}

    def _remember(self, manifest, placement=False, operation=False):
        directory = self._root / 'last_runs'
        directory.mkdir(parents=True, exist_ok=True)
        pointer = self._pointer(manifest)
        if operation or not pointer:
            pointer['run_dir'] = str(self._run_dir)
            if (manifest.get('active') or {}).get('mode') != 'reopen':
                pointer['last_attempt'] = str(self._run_dir)
        if placement and pointer.get('run_dir') == str(self._run_dir):
            # An older window may still poll its historical run. Only the
            # current operation may advance this floorplan's placement history.
            pointer['placement_dir'] = str(self._run_dir)
        key = self.project_path or 'snapshot:' + manifest['fingerprint']
        path = self._index(key)
        if not path.is_file() or json.loads(path.read_text()) != pointer:
            _write_json(path, pointer)

    def _locate(self, project):
        if self._run_dir is not None:
            return
        keys = [self.project_path] if self.project_path else []
        if project is not None:
            keys.append('snapshot:' + project_fingerprint(project))
        for key in keys:
            pointer = self._index(key)
            if not pointer.is_file():
                continue
            directory = Path(json.loads(pointer.read_text())['run_dir'])
            if not (directory / 'run.json').is_file():
                continue
            manifest = json.loads((directory / 'run.json').read_text())
            if manifest.get('project_path') not in (None, self.project_path):
                continue
            self._run_dir = directory
            if self.project_path and manifest.get('project_path') is None:
                manifest['project_path'] = self.project_path
                _write_json(directory / 'run.json', manifest)
                self._remember(manifest, placement=self._has_dump(directory), operation=True)
            return

    def last_run(self, project=None):
        self._locate(project)
        return self.poll(project) if self._run_dir else None

    def last_placement(self, project=None):
        self._locate(project)
        if self._run_dir is None:
            return None
        manifest = json.loads((self._run_dir / 'run.json').read_text())
        directory = self._pointer(manifest).get('placement_dir')
        if not directory and self._has_dump(self._run_dir):
            directory = str(self._run_dir)
        if not directory or not self._has_dump(directory):
            return None
        if Path(directory) == self._run_dir:
            return self.poll(project)
        previous = AlignRunner(self.config, self.project_path)
        previous._run_dir = Path(directory)
        previous._dump_cache = self._dump_cache
        return previous.poll(project)

    def rebind_project_path(self, path):
        """Transfer a Save As association without interrupting its worker."""
        target = str(Path(path).expanduser().resolve()) if path else None
        if target == self.project_path:
            return
        if self._run_dir is None:
            self.project_path = target
            return
        manifest = json.loads((self._run_dir / 'run.json').read_text())
        pointer = self._pointer(manifest)
        if target and self._index(target).is_file():
            existing = json.loads(self._index(target).read_text())
            directory = Path(existing['run_dir'])
            if directory != self._run_dir and (directory / 'run.json').is_file() and _alive(json.loads((directory / 'run.json').read_text()), directory):
                raise ValueError('The Save As target already has an active ALIGN run')
        for directory in {str(self._run_dir), pointer.get('placement_dir'), pointer.get('last_attempt')} - {None}:
            run_path = Path(directory) / 'run.json'
            if run_path.is_file():
                record = json.loads(run_path.read_text())
                if record.get('project_path') == self.project_path:
                    record['project_path'] = target
                    _write_json(run_path, record)
        self.project_path = target
        key = target or 'snapshot:' + manifest['fingerprint']
        self._index(key).parent.mkdir(parents=True, exist_ok=True)
        _write_json(self._index(key), dict(pointer, run_dir=str(self._run_dir)))

    def _active_index_run(self):
        """Check durable ownership even when this controller has cached history."""
        if self._run_dir is None:
            return None
        manifest = json.loads((self._run_dir / 'run.json').read_text())
        directory = Path(self._pointer(manifest).get('run_dir', self._run_dir))
        path = directory / 'run.json'
        if directory != self._run_dir and path.is_file():
            current = json.loads(path.read_text())
            if current.get('project_path') in (None, self.project_path) and _alive(current, directory):
                return directory
        return None

    def start(self, project):
        validate_ota_project(project)
        project.validate()
        self.config.validate()
        previous = self.last_run(project)
        if (previous and previous.status in _ACTIVE) or self._active_index_run() is not None:
            raise ValueError('Stop the current ALIGN run before generating another placement')
        self._root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix=project.top + '-', dir=self._root))
        try:
            # Use a detached snapshot, including incomplete/edit provenance data.
            snapshot = project.to_dict()
            detached = type(project).from_dict(snapshot)
            netlist, constraints = detached.export_align(directory / 'input')
            _write_json(directory / 'floorplan.json', snapshot)
            manifest = {'version': 1, 'profile_id': PROFILE_ID, 'project_path': self.project_path,
                        'fingerprint': project_fingerprint(detached), 'created_at': time.time(),
                        'top': detached.top, 'input_dir': str(netlist.parent),
                        'netlist': str(netlist), 'constraints': str(constraints),
                        'input_hashes': {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
                                         for p in (netlist, constraints, directory / 'floorplan.json')},
                        'generation_config': {'python': str(Path(self.config.python).expanduser().absolute()),
                            'backend_path': str(Path(self.config.backend_path).expanduser().resolve()),
                            'profile': PROFILE}, 'active': None}
            self._run_dir = directory
            _write_json(directory / 'run.json', manifest)
            self._remember(manifest, operation=True)
            return self._launch(manifest, 'generate')
        except Exception:
            # Retain failed setup artifacts for diagnosis, never overwrite inputs.
            raise

    def _launch(self, manifest, mode):
        token = uuid.uuid4().hex
        active = {'token': token, 'pid': None, 'mode': mode, 'stop_requested': None,
                  'python': str(Path(self.config.python).expanduser().absolute()),
                  'backend_path': str(Path(self.config.backend_path).expanduser().resolve()), 'port': self.config.port}
        manifest['active'] = active
        _write_json(self._run_dir / 'run.json', manifest)
        _write_json(self._run_dir / 'status.json', {'token': token, 'status': 'starting', 'url': None, 'error': None})
        self._remember(manifest, operation=True)
        command = [active['python'], '-I', '-u', str(Path(__file__).with_name('_align_worker.py').resolve()),
                   '--manifest', str(self._run_dir / 'run.json'), '--token', token]
        environment = dict(os.environ)
        environment.pop('ALIGN_HOME', None)
        environment.pop('PYTHONPATH', None)
        environment.pop('PYTHONHOME', None)
        try:
            with (self._run_dir / 'align.log').open('ab') as stream:
                self._process = subprocess.Popen(command, cwd=self._run_dir, env=environment,
                    stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            active['pid'] = self._process.pid
            _write_json(self._run_dir / 'run.json', manifest)
        except OSError as error:
            _write_json(self._run_dir / 'status.json', {'token': token, 'status': 'failed', 'url': None, 'error': str(error)})
        return self.poll()

    def open_last(self, project):
        previous = self.last_run(project)
        if previous is None:
            raise ValueError('No captured ALIGN placement exists for this floorplan')
        if previous.status in _ACTIVE:
            return previous
        current = self._active_index_run()
        if current is not None:
            # Explicit Open last reconnects to another instance's current run;
            # it must not start a second worker from this window's old cache.
            self._run_dir, self._process = current, None
            return self.poll(project)
        manifest = json.loads((self._run_dir / 'run.json').read_text())
        previous_directory = self._pointer(manifest).get('placement_dir')
        if previous_directory and self._has_dump(previous_directory):
            self._run_dir = Path(previous_directory)
            manifest = json.loads((self._run_dir / 'run.json').read_text())
        for relative, expected in manifest['input_hashes'].items():
            path = self._run_dir / relative
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError('Captured ALIGN input files changed; generate a fresh placement')
        if not (self._run_dir / '3_pnr' / '__placer_dump__.json').is_file():
            raise ValueError('This run has no generated placement to reopen')
        self.config.validate()
        self._launch(manifest, 'reopen')
        return self.poll(project)

    def poll(self, project=None):
        if self._run_dir is None:
            return None
        manifest = json.loads((self._run_dir / 'run.json').read_text())
        active = manifest.get('active') or {}
        progress = json.loads((self._run_dir / 'status.json').read_text()) if (self._run_dir / 'status.json').is_file() else {}
        if progress.get('token') != active.get('token'):
            progress = {'status': 'starting', 'url': None, 'error': None, 'token': active.get('token')}
        alive = _alive(manifest, self._run_dir)
        status = progress.get('status', 'starting')
        if self._process is not None:
            self._process.poll()  # reap exited children
        if active.get('stop_requested') and alive:
            status = 'stopping'
            if time.time() - active['stop_requested'] > 3:
                self._signal(manifest, signal.SIGKILL)
        elif not alive and status in _ACTIVE:
            code = self._process.returncode if self._process is not None else None
            status = 'stopped' if active.get('stop_requested') else ('completed' if code == 0 else 'failed')
            progress.update(status=status, url=None,
                error=None if status in ('stopped', 'completed') else 'ALIGN process exited before completing; see align.log')
            _write_json(self._run_dir / 'status.json', progress)
        if status in {'serving', 'completed', 'stopped', 'failed'} and self._has_dump(self._run_dir):
            self._remember(manifest, placement=True)
        url = progress.get('url')
        if status == 'serving':
            identity = (active.get('token'), url)
            if self._verified_ready != identity:
                if _ready(url):
                    self._verified_ready = identity
                else:
                    status, url = 'starting', None
        if status != 'serving':
            url = None
        return RunState(str(self._run_dir), status, url, progress.get('error'), str(self._run_dir / 'align.log'),
                        manifest['fingerprint'], project is not None and project_fingerprint(project) != manifest['fingerprint'],
                        alive and status != 'stopping', self._has_dump(self._run_dir) or
                        self._has_dump(self._pointer(manifest).get('placement_dir', self._run_dir)))

    def _signal(self, manifest, sig):
        if _alive(manifest, self._run_dir):
            pid = manifest['active']['pid']
            try:
                if os.getpgid(pid) == pid:
                    os.killpg(pid, sig)
            except ProcessLookupError:
                pass

    def stop(self):
        if self._run_dir is None:
            raise ValueError('No ALIGN run to stop')
        manifest = json.loads((self._run_dir / 'run.json').read_text())
        if _alive(manifest, self._run_dir):
            manifest['active']['stop_requested'] = time.time()
            _write_json(self._run_dir / 'run.json', manifest)
            self._signal(manifest, signal.SIGTERM)
        return self.poll()

    def shutdown(self):
        """Bounded final teardown; does not rely on a Qt/event-loop timer."""
        if self._run_dir is None:
            return None
        self.stop()
        manifest = json.loads((self._run_dir / 'run.json').read_text())
        deadline = time.monotonic() + 0.5
        while _alive(manifest, self._run_dir) and time.monotonic() < deadline:
            time.sleep(0.025)
        self._signal(manifest, signal.SIGKILL)
        if self._process is not None:
            try:
                self._process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
        return self.poll()

    close = shutdown

    def log_tail(self, max_chars=12000):
        if self._run_dir is None:
            return ''
        path = self._run_dir / 'align.log'
        if not path.is_file():
            return ''
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - max_chars * 4))
            return stream.read().decode('utf-8', errors='replace')[-max_chars:]
