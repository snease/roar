"""Isolated, persistent ALIGN placement runs; no Qt, browser, or ALIGN imports."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

PROFILE = json.loads(Path(__file__).with_name('align_profile.json').read_text(encoding='utf-8'))
PROFILE_ID = PROFILE['id']
_ACTIVE = {'starting', 'generating', 'serving', 'stopping'}
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


@contextmanager
def _ownership_lock(root):
    """Serialize index changes across processes, including unpublished PIDs."""
    # Editing and history inspection also work on hosts where launch is disabled.
    if sys.platform == 'linux':
        import fcntl
    else:
        fcntl = None
    with _LOCKS_GUARD:
        mutex, state = _LOCKS.setdefault(str(root), (threading.RLock(), threading.local()))
    with mutex:
        outer = not getattr(state, 'depth', 0)
        if outer:
            root.mkdir(parents=True, exist_ok=True)
            state.stream = (root / '.ownership.lock').open('a')
            if fcntl is not None:
                fcntl.flock(state.stream, fcntl.LOCK_EX)
        state.depth = getattr(state, 'depth', 0) + 1
        try:
            yield
        finally:
            state.depth -= 1
            if outer:
                if fcntl is not None:
                    fcntl.flock(state.stream, fcntl.LOCK_UN)
                state.stream.close()


def _serialized(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        if (method.__name__ in {'last_run', 'last_placement', 'last_preview', 'poll'} and self._run_dir is None
                and (not self.config.run_root or not self._root.is_dir())):
            return None
        with _ownership_lock(self._root):
            return method(self, *args, **kwargs)
    return guarded


def run_options(project):
    options = dict(PROFILE['run_defaults'], lambda_coeff=1.0)
    options.update(getattr(project, 'run_options', {}))
    allowed = {'nvariants', 'placer_sa_iterations', 'seed', 'lambda_coeff'}
    if set(options) - allowed:
        raise ValueError('Unknown ALIGN run options: ' + ', '.join(sorted(set(options) - allowed)))
    for key, low, high in [('nvariants', 1, 100), ('placer_sa_iterations', 1, 1000000), ('seed', 0, 2147483647)]:
        value = options[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'{key} must be an integer from {low} to {high}')
    value = options['lambda_coeff']
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0.01 <= value <= 1000:
        raise ValueError('Area / wirelength weight must be a finite number from 0.01 to 1000')
    return options


def _write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def project_fingerprint(project):
    """Identify generation inputs, independently of drawing and sizing provenance.

    This also works for incomplete drafts. Callers refresh live bindings before
    capturing a run; freshness checks never export files or resolve bindings.
    """
    devices = [{key: getattr(device, key) for key in
                  ('name', 'kind', 'terminals', 'model', 'width_um', 'length_um',
                   'nf', 'parameters', 'value')} for device in project.devices]
    for device in devices:
        for key in ('width_um', 'length_um'):
            if type(device[key]) in (int, float):
                device[key] = float(device[key])
    inputs = {'version': 2, 'top': project.top, 'ports': project.ports,
              'devices': devices,
              'constraints': project.active_constraints,
              'profile': project.align_profile, 'run_options': run_options(project)}
    data = json.dumps(inputs, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(data.encode()).hexdigest()


def _legacy_fingerprint(project):
    """Find unsaved captures made before sketch state was introduced."""
    state = project.to_dict()
    state.pop('sketch', None)
    state.pop('constraint_ids', None)
    state.pop('device_labels', None)
    state['version'] = 4
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def _tool_identity(config):
    # Backend source changes affect geometry even when its directory is the same.
    hashes = {}
    for name in PROFILE['backend_hashes']:
        try:
            hashes[name] = hashlib.sha256((Path(config.backend_path).expanduser() / name).read_bytes()).hexdigest()
        except OSError:
            hashes[name] = None
    packages = {}
    if config.python:
        prefix = Path(config.python).expanduser().absolute().parent.parent
        for metadata in prefix.glob('lib/python*/site-packages/align*.dist-info/METADATA'):
            try:
                packages[str(metadata.relative_to(prefix))] = hashlib.sha256(metadata.read_bytes()).hexdigest()
            except OSError:
                packages[str(metadata.relative_to(prefix))] = None
    return {'python': str(Path(config.python).expanduser().absolute()) if config.python else '',
            'backend_path': str(Path(config.backend_path).expanduser().resolve()) if config.backend_path else '',
            'backend_hashes': hashes, 'align_version': PROFILE['align_version'], 'packages': packages}


def validate_ota_project(project):
    """Validate the selected technology; retain the legacy public function name."""
    if getattr(project, 'align_profile', None) != PROFILE_ID:
        raise ValueError(f'Select layout technology {PROFILE["technology"]} in ALIGN settings')
    if not project.devices:
        raise ValueError('The selected floorplan has no devices to place')
    for device in project.devices:
        if device.kind != 'mos':
            raise ValueError(f'{device.name}: the selected ALIGN technology has no supported {device.kind} generator')
        if device.model.lower() not in PROFILE['models']:
            raise ValueError(f'{device.name}: unsupported device model {device.model!r} for the selected ALIGN technology')
        if len(device.terminals) != 4:
            raise ValueError(f'{device.name}: the selected ALIGN MOS generator requires four terminals')
        if device.nf is not None and (device.nf < 2 or device.nf % 2):
            raise ValueError(f'{device.name}: this SKY130 generator requires an even finger count (NF) of at least 2; edit the draft sizing')
        # Restrictions from the pinned gen_param.py and mos.py; not layout DRC.
        for label, value, minimum, maximum, quantum in (
                ('W per finger', device.width_um, 420, 2520, 10),
                ('L', device.length_um, 150, None, 5)):
            if value is None:
                continue  # project.validate reports unresolved dimensions.
            nm = value * 1000
            if not math.isfinite(nm) or nm < minimum - 1e-6 or (maximum is not None and nm > maximum + 1e-6):
                bounds = f'{minimum / 1000:g}–{maximum / 1000:g} µm' if maximum else f'at least {minimum / 1000:g} µm'
                raise ValueError(f'{device.name}: {label} must be {bounds} for this SKY130 generator')
            if abs(nm / quantum - round(nm / quantum)) > 1e-7:
                raise ValueError(f'{device.name}: {label} must be a multiple of {quantum / 1000:g} µm for this SKY130 generator')
    # The pinned placement backend snaps instance origins to the M1/M2
    # routing grid. Off-grid boundary halos shift every instance off that grid.
    for index, (constraint, enabled) in enumerate(zip(project.constraints, project._constraint_flags()), 1):
        if not enabled or constraint.get('constraint') != 'Boundary':
            continue
        for field, quantum in [('halo_horizontal', 0.43), ('halo_vertical', 0.42)]:
            value = constraint.get(field, 0)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                if abs(value / quantum - round(value / quantum)) > 1e-7:
                    raise ValueError(f'Constraint {index} (Boundary): {field.replace("_", " ")} must be a multiple of {quantum:g} µm for this SKY130 placement grid')
    run_options(project)


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
                raise ValueError(f'Backend does not match {PROFILE["backend_repository"]}@{PROFILE["backend_revision"]}: {name}. Select that revision’s SKY130_PDK subfolder; see Setup help in ALIGN settings.')
        if not self.run_root:
            raise ValueError('Select a directory for isolated ALIGN runs')
        try:
            root = Path(self.run_root).expanduser()
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=root):
                pass
        except OSError as error:
            raise ValueError(f'Cannot write to placement runs folder {self.run_root}: {error}. Choose a writable folder in ALIGN settings.') from error
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
    has_preview: bool = False


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
        request_url = urllib.parse.urlunparse(parsed._replace(path=parsed.path.rstrip('/') + '/_dash-layout', query=''))
        token = urllib.parse.parse_qs(parsed.query).get('token', [''])[0]
        request = urllib.request.Request(request_url, headers={'Authorization': 'Bearer ' + token})
        with opener.open(request, timeout=0.35) as response:
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
        self._preview_cache = {}

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

    def _has_preview(self, directory):
        path = Path(directory) / 'preview.json'
        try:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            cached = self._preview_cache.get(str(path))
            if cached and cached[0] == signature:
                return cached[1]
            from .geometry import load_preview
            load_preview(path)
            self._preview_cache[str(path)] = (signature, True)
            return True
        except (OSError, ValueError):
            return False

    def _stale(self, manifest, project):
        if project is None:
            return False
        if manifest.get('fingerprint_version') == 2:
            matches = project_fingerprint(project) == manifest['fingerprint']
        else:
            # Old captures hashed the complete authoring document. Compare their
            # immutable captured circuit with today's generation-only identity.
            try:
                snapshot = json.loads((self._run_dir / 'floorplan.json').read_text())
                matches = project_fingerprint(type(project).from_dict(snapshot)) == project_fingerprint(project)
            except (OSError, ValueError, KeyError, TypeError):
                matches = False
        captured_tool = manifest.get('generation_config', {}).get('tool_identity')
        return not matches or (captured_tool is not None and captured_tool != _tool_identity(self.config))

    def _pointer(self, manifest):
        key = self.project_path or 'snapshot:' + manifest['fingerprint']
        path = self._index(key)
        return json.loads(path.read_text()) if path.is_file() else {}

    def _remember(self, manifest, placement=False, operation=False, preview=False):
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
        if preview and pointer.get('run_dir') == str(self._run_dir):
            pointer['preview_dir'] = str(self._run_dir)
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
            keys.append('snapshot:' + _legacy_fingerprint(project))
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
                target = self.project_path
                self.project_path = None
                try:
                    # Adopt the whole unsaved history, including a successful
                    # preview preceding the latest failed/cancelled attempt.
                    self.rebind_project_path(target)
                finally:
                    self.project_path = target
            return

    @_serialized
    def last_run(self, project=None):
        self._locate(project)
        return self.poll(project) if self._run_dir else None

    @_serialized
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

    @_serialized
    def last_preview(self, project=None):
        """Return the latest successful physical preview, including after failure."""
        self._locate(project)
        if self._run_dir is None:
            return None
        manifest = json.loads((self._run_dir / 'run.json').read_text())
        directory = self._pointer(manifest).get('preview_dir')
        if not directory and self._has_preview(self._run_dir):
            directory = str(self._run_dir)
        if not directory or not self._has_preview(directory):
            return None
        if Path(directory) == self._run_dir:
            return self.poll(project)
        previous = AlignRunner(self.config, self.project_path)
        previous._run_dir = Path(directory)
        previous._preview_cache = self._preview_cache
        return previous.poll(project)

    def _check_save_as(self, path):
        target = str(Path(path).expanduser().resolve()) if path else None
        if target and self._index(target).is_file():
            existing = json.loads(self._index(target).read_text())
            directory = Path(existing['run_dir'])
            record = directory / 'run.json'
            if directory != self._run_dir and record.is_file() and _alive(json.loads(record.read_text()), directory):
                raise ValueError('The Save As target already has an active ALIGN run')

    @contextmanager
    def save_as_transaction(self, path):
        """Reserve ownership across a caller's atomic save, before rebinding."""
        target = str(Path(path).expanduser().resolve())
        if (self._run_dir is None and (not self.config.run_root or not any(
                self._index(key).is_file() for key in (self.project_path, target) if key))):
            # A draft with no recorded history needs no ALIGN storage, even
            # when remembered tool paths or the runs directory are unavailable.
            # Existing source/target records still use the ownership transaction.
            yield
            self.project_path = target
            return
        with _ownership_lock(self._root):
            self._check_save_as(path)
            original_path = self.project_path
            target = Path(path).expanduser().resolve()
            records = {target, self._index(str(target))}
            if self._run_dir is not None:
                manifest = json.loads((self._run_dir / 'run.json').read_text())
                pointer = self._pointer(manifest)
                records.update(Path(directory) / 'run.json' for directory in
                    {str(self._run_dir), pointer.get('placement_dir'), pointer.get('preview_dir'), pointer.get('last_attempt')} - {None})
            originals = {record: (record.read_bytes(), record.stat().st_mode & 0o777) if record.is_file() else None for record in records}
            try:
                yield
                self.rebind_project_path(path)
            except Exception:
                # An I/O failure during the ownership commit must not leave a
                # saved document associated with half-transferred run history.
                self.project_path = original_path
                for record, saved in originals.items():
                    if saved is None:
                        record.unlink(missing_ok=True)
                    else:
                        contents, permissions = saved
                        temporary = record.with_name(record.name + '.' + uuid.uuid4().hex + '.rollback')
                        try:
                            temporary.write_bytes(contents)
                            temporary.chmod(permissions)
                            os.replace(temporary, record)
                        finally:
                            temporary.unlink(missing_ok=True)
                raise

    @_serialized
    def rebind_project_path(self, path):
        """Transfer a Save As association without interrupting its worker."""
        target = str(Path(path).expanduser().resolve()) if path else None
        if target == self.project_path:
            return
        self._check_save_as(path)
        if self._run_dir is None:
            self.project_path = target
            return
        manifest = json.loads((self._run_dir / 'run.json').read_text())
        pointer = self._pointer(manifest)
        for directory in {str(self._run_dir), pointer.get('placement_dir'), pointer.get('preview_dir'), pointer.get('last_attempt')} - {None}:
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

    def start(self, project, *, preview=False):
        validate_ota_project(project)
        project.validate()
        self.config.validate()
        with _ownership_lock(self._root):
            return self._start_locked(project, preview=preview)

    def _start_locked(self, project, *, preview=False):
        previous = self.last_run(project)
        if (previous and previous.status in _ACTIVE) or self._active_index_run() is not None:
            raise ValueError('Stop the current ALIGN run before generating another placement')
        history = None
        if self.project_path is None and self._run_dir is not None:
            history = self._pointer(json.loads((self._run_dir / 'run.json').read_text()))
        self._root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix=project.top + '-', dir=self._root))
        try:
            # Use a detached snapshot, including incomplete/edit provenance data.
            snapshot = project.to_dict()
            detached = type(project).from_dict(snapshot)
            netlist, constraints = detached.export_align(directory / 'input')
            _write_json(directory / 'floorplan.json', snapshot)
            manifest = {'version': 2, 'profile_id': PROFILE_ID, 'project_path': self.project_path,
                        'fingerprint': project_fingerprint(detached), 'created_at': time.time(),
                        'fingerprint_version': 2,
                        'top': detached.top, 'input_dir': str(netlist.parent),
                        'constraint_rows': [i + 1 for i, enabled in enumerate(snapshot.get('constraint_enabled', [True] * len(detached.constraints))) if enabled],
                        'netlist': str(netlist), 'constraints': str(constraints),
                        'input_hashes': {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
                                         for p in (netlist, constraints, directory / 'floorplan.json')},
                        'generation_config': {'python': str(Path(self.config.python).expanduser().absolute()),
                            'backend_path': str(Path(self.config.backend_path).expanduser().resolve()),
                            'profile': PROFILE, 'run_options': run_options(detached),
                            'tool_identity': _tool_identity(self.config)}, 'active': None}
            self._run_dir = directory
            _write_json(directory / 'run.json', manifest)
            if history:
                # An unsaved draft changes snapshot keys when inputs change.
                # Its last valid outputs still belong to this editing session.
                _write_json(self._index('snapshot:' + manifest['fingerprint']), history)
            self._remember(manifest, operation=True)
            return self._launch(manifest, 'preview' if preview else 'generate')
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

    @_serialized
    def open_last(self, project=None):
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

    def _process_alive(self, manifest):
        active = manifest.get('active') or {}
        if self._process is not None and self._process.pid == active.get('pid'):
            # waitpid owns the authoritative state for our direct child. A
            # transient /proc read must not declare a still-running worker dead.
            return self._process.poll() is None
        return _alive(manifest, self._run_dir)

    @_serialized
    def poll(self, project=None):
        if self._run_dir is None:
            return None
        manifest = json.loads((self._run_dir / 'run.json').read_text())
        active = manifest.get('active') or {}
        progress = json.loads((self._run_dir / 'status.json').read_text()) if (self._run_dir / 'status.json').is_file() else {}
        if progress.get('token') != active.get('token'):
            progress = {'status': 'starting', 'url': None, 'error': None, 'token': active.get('token')}
        alive = self._process_alive(manifest)
        status = progress.get('status', 'starting')
        if active.get('stop_requested') and alive:
            status = 'stopping'
            if time.time() - active['stop_requested'] > 3:
                self._signal(manifest, signal.SIGKILL)
        elif not alive and status in _ACTIVE:
            # The worker may have published its specific failure/stop between
            # the initial status read and process exit. Never overwrite it.
            latest = json.loads((self._run_dir / 'status.json').read_text())
            if latest.get('token') == active.get('token') and latest.get('status') not in _ACTIVE:
                progress, status = latest, latest.get('status')
            else:
                code = self._process.returncode if self._process is not None else None
                status = 'stopped' if active.get('stop_requested') else ('completed' if code == 0 else 'failed')
                if code is not None and code < 0:
                    reason = 'ALIGN worker was terminated by ' + signal.Signals(-code).name
                else:
                    reason = 'ALIGN worker exited' + (f' with code {code}' if code is not None else '')
                progress.update(status=status, url=None,
                    error=None if status in ('stopped', 'completed') else f'{reason}. See {self._run_dir / "align.log"}')
                _write_json(self._run_dir / 'status.json', progress)
        if status in {'serving', 'completed', 'stopped', 'failed'} and self._has_dump(self._run_dir):
            self._remember(manifest, placement=True)
        if status in {'completed', 'stopped', 'failed'} and self._has_preview(self._run_dir):
            self._remember(manifest, preview=True)
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
                        manifest['fingerprint'], self._stale(manifest, project),
                        alive and status != 'stopping', self._has_dump(self._run_dir) or
                        self._has_dump(self._pointer(manifest).get('placement_dir', self._run_dir)),
                        self._has_preview(self._run_dir) or
                        self._has_preview(self._pointer(manifest).get('preview_dir', self._run_dir)))

    def _signal(self, manifest, sig):
        if self._process_alive(manifest):
            pid = manifest['active']['pid']
            try:
                if os.getpgid(pid) == pid:
                    os.killpg(pid, sig)
            except ProcessLookupError:
                pass

    @_serialized
    def stop(self):
        if self._run_dir is None:
            raise ValueError('No ALIGN run to stop')
        manifest = json.loads((self._run_dir / 'run.json').read_text())
        if self._process_alive(manifest):
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
        while self._process_alive(manifest) and time.monotonic() < deadline:
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
