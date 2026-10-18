"""Bounded decoding of auxiliary run history and authoritative worker records."""
from __future__ import annotations

import json
import math
from pathlib import Path
import os


class RecordError(ValueError):
    """An existing record cannot safely establish run identity or ownership."""


def _text(value, limit=4096):
    return isinstance(value, str) and 0 < len(value) <= limit and '\0' not in value


def _path(value):
    return _text(value) and Path(value).is_absolute()


def _digest(value):
    return _text(value, 64) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _number(value):
    return (type(value) in (int, float) and 0 <= value <= 10**12
            and (type(value) is int or math.isfinite(value)))


def _valid(record, kind):
    if not isinstance(record, dict):
        return False
    version = record.get('version', None if kind == 'run' else 1)
    if type(version) is not int or version not in ((1, 2) if kind == 'run' else (1,)):
        return False
    if kind == 'index':
        fields = {'run_dir', 'placement_dir', 'preview_dir', 'last_attempt'}
        return (_path(record.get('run_dir')) and not set(record) - fields - {'version'}
                and all(_path(record[key]) for key in fields & record.keys()))
    if kind == 'status':
        return (_text(record.get('token'), 128)
                and record.get('status') in {'starting', 'generating', 'serving', 'stopping', 'completed', 'stopped', 'failed'}
                and (record.get('url') is None or _text(record['url']))
                and (record.get('error') is None or _text(record['error'], 32768)))
    if (not _digest(record.get('fingerprint')) or not _text(record.get('top'), 256)
            or not _text(record.get('profile_id'), 256)
            or not _number(record.get('created_at'))
            or 'project_path' not in record
            or (record['project_path'] is not None and not _path(record['project_path']))
            or any(not _path(record.get(key)) for key in ('input_dir', 'netlist', 'constraints'))
            or type(record.get('fingerprint_version', 1)) is not int
            or record.get('fingerprint_version', 1) not in (1, 2)):
        return False
    hashes = record.get('input_hashes')
    if not isinstance(hashes, dict) or not 1 <= len(hashes) <= 256:
        return False
    if any(not _text(name) or Path(name).is_absolute() or '..' in Path(name).parts
           or not _digest(digest) for name, digest in hashes.items()):
        return False
    config = record.get('generation_config')
    if not isinstance(config, dict) or not all(_path(config.get(key)) for key in ('python', 'backend_path')):
        return False
    if 'tool_identity' in config and not isinstance(config['tool_identity'], dict):
        return False
    active = record.get('active')
    if active is None:
        return 'active' in record
    if not isinstance(active, dict):
        return False
    pid = active.get('pid')
    return (_text(active.get('token'), 128)
            and (pid is None or type(pid) is int and 0 < pid <= 2**31 - 1)
            and active.get('mode') in ('generate', 'preview', 'reopen')
            and all(_path(active.get(key)) for key in ('python', 'backend_path'))
            and type(active.get('port')) is int and 0 <= active['port'] <= 65535
            and (active.get('stop_requested') is None or _number(active['stop_requested'])))


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('nonfinite number')
    return result


def _invalid_constant(value):
    raise ValueError(f'nonfinite number: {value}')


def read_record(path, kind, *, recover=False):
    """Read a record; optional history is ignored, ownership errors are explicit.

    Index/status records predate schema version fields and remain version 1.
    Run manifests support the existing versions 1 and 2. Files are never changed
    by reading, including when a record is missing or malformed.
    """
    path = Path(path)
    limit = {'index': 16384, 'status': 65536, 'run': 2 * 1024 * 1024}[kind]
    try:
        with path.open('rb') as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError('record exceeds size limit')
        record = json.loads(data, parse_constant=_invalid_constant, parse_float=_finite_float)
        if not _valid(record, kind):
            raise ValueError('unsupported version, shape, or fields')
        return record
    except (OSError, ValueError, TypeError, RecursionError) as error:
        if recover:
            return None
        raise RecordError(f'Cannot safely use ALIGN {kind} record {path}: {error}. '
                          'The original record is preserved; inspect it before recovering this run.') from error


def preserve_invalid_record(path):
    """Before a deliberate write, retain unusable auxiliary evidence beside it."""
    kind = 'run' if path.name == 'run.json' else 'status' if path.name == 'status.json' else 'index' if path.parent.name == 'last_runs' else None
    if kind and path.is_file() and read_record(path, kind, recover=True) is None:
        # Atomic writers replace inodes; a hard link retains the old evidence
        # without copying an arbitrarily oversized corrupt record. Repeated
        # failed writes to the same inode reuse its existing evidence link.
        info = path.stat()
        backup = path.with_name(f'{path.name}.invalid-{info.st_dev:x}-{info.st_ino:x}')
        try:
            os.link(path, backup)
        except FileExistsError:
            pass
