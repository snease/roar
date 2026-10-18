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

from .run_options import PROFILE, default_run_options, validate_run_options
from .run_records import read_record, RecordError, preserve_invalid_record

PROFILE_ID = PROFILE['id']
_ACTIVE = {'starting', 'generating', 'serving', 'stopping'}
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def managed_installation(settings):
    """Legacy saved paths cannot silently replace the bundled installation."""
    custom = str(settings.get('custom_installation', False)).lower() in ('true', '1')
    return os.environ.get('ROAR_MANAGED_RUNTIME') == '1' and not custom


def align_process_environment(python):
    """Keep the GUI's native libraries out of the isolated ALIGN worker.

    A custom installation owns its own runtime. Only the bundled interpreter
    gets the managed ALIGN prefix; do not infer a library path from user input.
    """
    environment = dict(os.environ)
    for name in ('ALIGN_HOME', 'PYTHONPATH', 'PYTHONHOME'):
        environment.pop(name, None)
    if environment.get('ROAR_MANAGED_RUNTIME') == '1':
        for name in ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'CONDA_PREFIX', 'CONDA_DEFAULT_ENV', 'VIRTUAL_ENV'):
            environment.pop(name, None)
        managed = environment.get('ROAR_ALIGN_PYTHON', '')
        if managed and Path(python).expanduser().absolute() == Path(managed).absolute():
            prefix = Path(managed).parent.parent
            environment['LD_LIBRARY_PATH'] = str(prefix / 'lib')
            environment['CONDA_PREFIX'] = str(prefix)
            environment['PATH'] = str(prefix / 'bin') + os.pathsep + os.defpath
    return environment


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
        if method.__name__ in {'shutdown', 'log_tail'} and self._run_dir is None:
            return method(self, *args, **kwargs)
        if (method.__name__ in {'snapshot', 'poll'} and self._run_dir is None
                and (not self.config.run_root or not (self._root / 'last_runs').is_dir())):
            return RunSnapshot() if method.__name__ == 'snapshot' else None
        with _ownership_lock(self._root):
            try:
                return method(self, *args, **kwargs)
            except RecordError:
                # Optional history never blocks authoring. Retain cached identity
                # so mutation methods still reject unsafe ownership records.
                if method.__name__ in {'snapshot', 'poll', 'log_tail'}:
                    if method.__name__ == 'snapshot':
                        return RunSnapshot()
                    return '' if method.__name__ == 'log_tail' else None
                raise
    return guarded


def run_options(project):
    options = default_run_options()
    options.update(getattr(project, 'run_options', {}))
    return validate_run_options(options)


def _write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        preserve_invalid_record(path)
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

    @classmethod
    def from_settings(cls, settings, defaults=None):
        managed = defaults is None and managed_installation(settings)
        defaults = defaults or cls.from_environment()
        return cls(
            defaults.python if managed else str(settings.get('python', defaults.python)),
            defaults.backend_path if managed else str(settings.get('backend_path', defaults.backend_path)),
            str(settings.get('run_root', defaults.run_root)), defaults.port)

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
        if type(self.port) is not int or not 0 <= self.port <= 65535:
            raise ValueError('ALIGN port must be 0 (automatic) or an integer in 1..65535')

    def prepare_storage(self):
        """Check writable run storage only when an operation will write to it."""
        if not self.run_root:
            raise ValueError('Select a directory for isolated ALIGN runs')
        try:
            root = Path(self.run_root).expanduser()
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=root):
                pass
        except OSError as error:
            raise ValueError(f'Cannot write to placement runs folder {self.run_root}: {error}. Choose a writable folder in ALIGN settings.') from error


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


@dataclass(frozen=True)
class RunSnapshot:
    current: RunState | None = None
    placement: RunState | None = None
    preview: RunState | None = None


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
    try:
        parsed = urllib.parse.urlparse(url or '')
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port:
            return False
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

    def _stale(self, manifest, project, directory, freshness):
        if project is None:
            return False
        if manifest.get('fingerprint_version') == 2:
            matches = freshness[0] == manifest['fingerprint']
        else:
            # Old captures hashed the complete authoring document. Compare their
            # immutable captured circuit with today's generation-only identity.
            try:
                snapshot = json.loads((directory / 'floorplan.json').read_text())
                matches = project_fingerprint(type(project).from_dict(snapshot)) == freshness[0]
            except (OSError, ValueError, KeyError, TypeError):
                matches = False
        captured_tool = manifest.get('generation_config', {}).get('tool_identity')
        return not matches or (captured_tool is not None and captured_tool != freshness[1])

    def _read_pointer(self, key, owners, *, strict=False):
        """Ignore history entries transferred away from this index's owner."""
        path = self._index(key)
        pointer = read_record(path, 'index', recover=not (strict and path.exists())) or {}
        for field in ('run_dir', 'placement_dir', 'preview_dir', 'last_attempt'):
            if field not in pointer:
                continue
            record = Path(pointer[field]) / 'run.json' if pointer[field] else None
            if record is None or not record.is_file():
                del pointer[field]
                continue
            manifest = read_record(record, 'run', recover=not (strict and field == 'run_dir'))
            if manifest is None or manifest.get('project_path') not in owners:
                del pointer[field]
        return pointer

    def _pointer(self, manifest, *, strict=False):
        key = self.project_path or 'snapshot:' + manifest['fingerprint']
        return self._read_pointer(key, (self.project_path,), strict=strict)

    def _remember(self, manifest, placement=False, operation=False, preview=False):
        directory = self._root / 'last_runs'
        directory.mkdir(parents=True, exist_ok=True)
        pointer = self._pointer(manifest, strict=True)
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
        if read_record(path, 'index', recover=True) != pointer:
            _write_json(path, pointer)

    def _locate(self, project, fingerprint=None, *, adopt=True):
        if self._owned_manifest() is not None:
            return
        keys = [self.project_path] if self.project_path else []
        if project is not None:
            keys.append('snapshot:' + (fingerprint or project_fingerprint(project)))
            keys.append('snapshot:' + _legacy_fingerprint(project))
        for key in keys:
            pointer = self._read_pointer(key, (None, self.project_path))
            if not pointer.get('run_dir'):
                continue
            directory = Path(pointer['run_dir'])
            manifest = read_record(directory / 'run.json', 'run')
            if manifest.get('project_path') not in (None, self.project_path):
                continue
            if self.project_path and manifest.get('project_path') is None and not adopt:
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

    def _owned_manifest(self):
        """Check cached ownership while the caller holds the ownership lock."""
        if self._run_dir is None:
            return None
        manifest = read_record(self._run_dir / 'run.json', 'run')
        if manifest.get('project_path') == self.project_path:
            return manifest
        # Another controller transferred this run. A direct child still needs
        # reaping when its new owner stops it, without retaining control here.
        process = self._process
        self._run_dir = self._process = self._verified_ready = None
        if process is not None:
            threading.Thread(target=process.wait, daemon=True).start()
        return None

    @_serialized
    def snapshot(self, project=None, *, advance=True):
        """Advance one worker once, then describe its history without writes."""
        fingerprint = project_fingerprint(project) if project is not None else None
        self._locate(project, fingerprint, adopt=advance)
        manifest = self._owned_manifest()
        if manifest is None:
            return RunSnapshot()
        # Corrupt ownership evidence cannot authorize process controls, even
        # while optional reads remain available to the rest of the editor.
        pointer = self._pointer(manifest, strict=True)
        freshness = (fingerprint, _tool_identity(self.config)) if project is not None else None
        progress, alive = self._advance_run(manifest) if advance else (None, None)
        pointer = self._pointer(manifest, strict=True) if advance else pointer
        current = self._describe_run(self._run_dir, manifest, project, freshness,
                                     pointer, progress=progress, alive=alive)

        descriptions = {self._run_dir: current}

        def historical(field, predicate):
            directory = pointer.get(field)
            if not directory and predicate(self._run_dir):
                directory = str(self._run_dir)
            if not directory or not predicate(directory):
                return None
            directory = Path(directory)
            if directory in descriptions:
                return descriptions[directory]
            record = read_record(directory / 'run.json', 'run', recover=True)
            if record is None or record['project_path'] != self.project_path:
                return None
            descriptions[directory] = self._describe_run(directory, record, project, freshness, pointer)
            return descriptions[directory]

        return RunSnapshot(current, historical('placement_dir', self._has_dump),
                           historical('preview_dir', self._has_preview))

    def last_run(self, project=None):
        return self.snapshot(project).current

    def last_placement(self, project=None):
        return self.snapshot(project).placement

    def last_preview(self, project=None):
        """Return the latest successful physical preview, including after failure."""
        return self.snapshot(project).preview

    def _check_save_as(self, path):
        target = str(Path(path).expanduser().resolve()) if path else None
        existing = self._read_pointer(target, (None, target), strict=True) if target else {}
        if existing.get('run_dir'):
            directory = Path(existing['run_dir'])
            record = directory / 'run.json'
            if directory != self._run_dir and record.is_file():
                manifest = read_record(record, 'run')
                if manifest.get('project_path') in (None, target) and _alive(manifest, directory):
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
            self._owned_manifest()
            self._check_save_as(path)
            original_path = self.project_path
            target = Path(path).expanduser().resolve()
            records = {target, self._index(str(target))}
            if self._run_dir is not None:
                manifest = read_record(self._run_dir / 'run.json', 'run')
                pointer = self._pointer(manifest, strict=True)
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
        manifest = self._owned_manifest()
        target = str(Path(path).expanduser().resolve()) if path else None
        if target == self.project_path:
            return
        self._check_save_as(path)
        if self._run_dir is None:
            self.project_path = target
            return
        pointer = self._pointer(manifest, strict=True)
        for directory in {str(self._run_dir), pointer.get('placement_dir'), pointer.get('preview_dir'), pointer.get('last_attempt')} - {None}:
            run_path = Path(directory) / 'run.json'
            if run_path.is_file():
                record = read_record(run_path, 'run')
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
        manifest = read_record(self._run_dir / 'run.json', 'run')
        directory = Path(self._pointer(manifest, strict=True).get('run_dir', self._run_dir))
        path = directory / 'run.json'
        if directory != self._run_dir and path.is_file():
            current = read_record(path, 'run')
            if current.get('project_path') in (None, self.project_path) and _alive(current, directory):
                return directory
        return None

    def _check_history_ownership(self, project):
        """A known current pointer cannot authorize replacing unknown ownership."""
        keys = [self.project_path] if self.project_path else []
        if project is not None:
            keys.extend(('snapshot:' + project_fingerprint(project),
                         'snapshot:' + _legacy_fingerprint(project)))
        for key in keys:
            self._read_pointer(key, (None, self.project_path), strict=True)

    def start(self, project):
        validate_ota_project(project)
        project.validate()
        self.config.validate()
        self.config.prepare_storage()
        with _ownership_lock(self._root):
            return self._start_locked(project)

    def _start_locked(self, project):
        self._owned_manifest()
        self._check_history_ownership(project)
        previous = self.last_run(project)
        if (previous and previous.status in _ACTIVE) or self._active_index_run() is not None:
            raise ValueError('Stop the current ALIGN run before generating another placement')
        history = None
        if self.project_path is None and self._run_dir is not None:
            history = self._pointer(read_record(self._run_dir / 'run.json', 'run'), strict=True)
        self._root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix=project.top + '-', dir=self._root))
        # This capture becomes a persisted document: ensure it can reopen with
        # the same source/override semantics before publishing a run.
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
        return self._launch(manifest, 'preview')

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
        environment = align_process_environment(self.config.python)
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
        self._owned_manifest()
        self._check_history_ownership(project)
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
        manifest = read_record(self._run_dir / 'run.json', 'run')
        previous_directory = self._pointer(manifest).get('placement_dir')
        if previous_directory and self._has_dump(previous_directory):
            directory = Path(previous_directory)
            manifest = read_record(directory / 'run.json', 'run')
            if manifest.get('project_path') != self.project_path:
                raise ValueError('No captured ALIGN placement exists for this floorplan')
            self._run_dir = directory
        for relative, expected in manifest['input_hashes'].items():
            path = self._run_dir / relative
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError('Captured ALIGN input files changed; generate a fresh placement')
        if not (self._run_dir / '3_pnr' / '__placer_dump__.json').is_file():
            raise ValueError('This run has no generated placement to reopen')
        self.config.validate()
        self.config.prepare_storage()
        self._launch(manifest, 'reopen')
        return self.poll(project)

    def _process_alive(self, manifest):
        active = manifest.get('active') or {}
        if self._process is not None and self._process.pid == active.get('pid'):
            # waitpid owns the authoritative state for our direct child. A
            # transient /proc read must not declare a still-running worker dead.
            return self._process.poll() is None
        return _alive(manifest, self._run_dir)

    def _advance_run(self, manifest):
        """Reap/publish current process state; caller holds the ownership lock."""
        active = manifest.get('active') or {}
        progress = read_record(self._run_dir / 'status.json', 'status', recover=True) or {}
        if not active.get('token'):
            progress = {'status': 'failed', 'url': None, 'error': 'ALIGN run setup did not start a worker'}
        elif progress.get('token') != active.get('token'):
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
            latest = read_record(self._run_dir / 'status.json', 'status', recover=True) or {}
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
        progress = dict(progress, status=status, url=url)
        return progress, alive

    def _describe_run(self, directory, manifest, project, freshness, pointer, *, progress=None, alive=None):
        """Read an immutable view; never poll a child or update history/status."""
        directory = Path(directory)
        active = manifest.get('active') or {}
        if progress is None:
            progress = read_record(directory / 'status.json', 'status', recover=True) or {}
            if not active.get('token'):
                progress = {'status': 'failed', 'error': 'ALIGN run setup did not start a worker'}
            elif progress.get('token') != active['token']:
                progress = {'status': 'starting'}
        if alive is None:
            alive = _alive(manifest, directory)
        status = progress.get('status', 'starting')
        if alive and active.get('stop_requested'):
            status = 'stopping'
        elif not alive and status in _ACTIVE:
            status = 'stopped' if active.get('stop_requested') else 'failed'
        url = progress.get('url') if status == 'serving' else None
        return RunState(str(directory), status, url, progress.get('error'), str(directory / 'align.log'),
                        manifest['fingerprint'], self._stale(manifest, project, directory, freshness),
                        alive and status != 'stopping', self._has_dump(directory) or
                        self._has_dump(pointer.get('placement_dir', directory)),
                        self._has_preview(directory) or self._has_preview(pointer.get('preview_dir', directory)))

    @_serialized
    def poll(self, project=None):
        manifest = self._owned_manifest()
        if manifest is None:
            return None
        self._pointer(manifest, strict=True)
        progress, alive = self._advance_run(manifest)
        freshness = (project_fingerprint(project), _tool_identity(self.config)) if project is not None else None
        return self._describe_run(self._run_dir, manifest, project, freshness,
                                  self._pointer(manifest), progress=progress, alive=alive)

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
        manifest = self._owned_manifest()
        if manifest is None:
            raise ValueError('No ALIGN run to stop')
        if self._process_alive(manifest):
            manifest['active']['stop_requested'] = time.time()
            _write_json(self._run_dir / 'run.json', manifest)
            self._signal(manifest, signal.SIGTERM)
        return self.poll()

    @_serialized
    def shutdown(self):
        """Bounded final teardown; does not rely on a Qt/event-loop timer."""
        if self._owned_manifest() is None:
            return None
        self.stop()
        manifest = read_record(self._run_dir / 'run.json', 'run')
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

    @_serialized
    def log_tail(self, max_chars=12000):
        if self._owned_manifest() is None:
            return ''
        path = self._run_dir / 'align.log'
        if not path.is_file():
            return ''
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - max_chars * 4))
            return stream.read().decode('utf-8', errors='replace')[-max_chars:]
