#!/usr/bin/env python3
"""Install the pinned ALIGN backend and viewer assets in the managed runtime.

Run with the ALIGN environment's Python. No package manager, compiler, Qt, or
user settings are involved. --check performs no downloads or filesystem writes.
"""
import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request


# The runtime profile owns the revision and generator hashes used by both
# interpreters. Additional provisioning-only files below are checked at setup.
PROFILE = json.loads((Path(__file__).resolve().parents[1] /
                      'src/floorplan/align_profile.json').read_text(encoding='utf-8'))
BACKEND_REVISION = PROFILE['backend_revision']
BACKEND_URL = PROFILE['backend_repository'].replace('https://github.com/',
    'https://codeload.github.com/') + '/tar.gz/' + BACKEND_REVISION
BACKEND_SHA256 = '1c7311c4016d8afcb73a9b966917cf4570b7bc1fe76e07bcf04d6cb1a2a5d705'
BACKEND_FILES = {
    **PROFILE['backend_hashes'],
    'Align_primitives.py': '886938e72b7cfc47390a8ac5f3f04c4f9b4e19487f13baf340d889ab6b41107d',
    'LICENSE': '9da4c466377398db5b1453c54f64e6b175243a3869fa5bf65c26217b7c2ea4fc',
    '__init__.py': 'd6207ba4e9a22e787c20a6e4df71fde2ca3ed05884c1a8251bf934d7c50e96e7',
    'cap.py': 'ce98564c4f5302c90278871d116340fdd416cb001392b37efa96625f0357687a',
    'fabric_Cap.py': '6b8aa54cdc30ce8a04469fde58fc531e725a86bd2727f8b464e0ee4bb5fcbd64',
    'fabric_Res.py': '7dfa23938ffa051c97b944624805a2cbd08e7ba1fbd3f66af09d0e44186d5284',
    'fabric_ring.py': 'eedce84e6c7ff7554b0a8e76492366d24cc67620fe6b9a7cd4956e39f2d54294',
    'guard_ring.py': 'd91b19528bc90350557e661eb7b64ef005320aa59f2ba0fef73c1aaaf1659114',
    'res.py': '7e50d16b7098bdd5cd14aa82f699d9e93764abeab8bbc3b5dd65c5fb0a188e9c',
}
ASSETS_REVISION = '8d3cc2e9aa5511315203eb5cbfd00d2c88fdbd18'
ASSETS_URL = 'https://raw.githubusercontent.com/ALIGN-analoglayout/ALIGN-public/' + ASSETS_REVISION + '/align/gui/assets/'
ASSET_FILES = {
    '00_from_codepen.css': 'bad94119bbca0db279a45210b87404701260688287f4c288f30f9d549ed8874f',
    '01_style.css': '90220346ea169bbc47bad269f2b017d13a4453743ed049aaefb636ce42eab57a',
    '02_typography.css': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
    'align.png': '50bd9907748139b09ecf68cd2bb2e99538f488c8488b3e35cbcf8a8f41b51eb0',
}
_MARKER = '.roar-managed-backend.json'


def _download(url, expected):
    request = urllib.request.Request(url, headers={'User-Agent': 'ROAR-runtime-setup'})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError('Download checksum does not match the pinned release: ' + url)
    return data


def _matches(path, expected):
    try:
        return not path.is_symlink() and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected
    except OSError:
        return False


def _complete(directory, hashes):
    return not directory.is_symlink() and all(_matches(directory / name, digest) for name, digest in hashes.items())


def _managed(directory):
    try:
        marker = directory / _MARKER
        return not marker.is_symlink() and json.loads(marker.read_text()).get('managed_by') == 'ROAR ALIGN setup'
    except (OSError, ValueError, AttributeError):
        return False


def provision_backend(target, *, check=False):
    target = Path(target).expanduser().absolute()
    if _complete(target, BACKEND_FILES):
        return target
    if check:
        raise ValueError('Managed SKY130 backend is missing or changed; rerun ROAR setup.')
    if target.is_symlink() or (target.exists() and not _managed(target)):
        raise ValueError('Refusing to replace an unrecognized backend directory: ' + str(target))
    payload = _download(BACKEND_URL, BACKEND_SHA256)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.roar-backend-', dir=target.parent) as work:
        stage, backup = Path(work) / 'new', Path(work) / 'previous'
        stage.mkdir()
        # Extract only the named source/data files, never archive paths, links,
        # executables, or upstream's committed Python bytecode.
        with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
            root = 'ALIGN-pdk-sky130-' + BACKEND_REVISION + '/'
            for name, expected in BACKEND_FILES.items():
                member = archive.getmember(root + (name if name == 'LICENSE' else 'SKY130_PDK/' + name))
                if not member.isfile():
                    raise ValueError('Backend archive contains a link or special file: ' + name)
                data = archive.extractfile(member).read()
                if hashlib.sha256(data).hexdigest() != expected:
                    raise ValueError('Backend file checksum mismatch: ' + name)
                (stage / name).write_bytes(data)
        (stage / _MARKER).write_text(json.dumps({'managed_by': 'ROAR ALIGN setup',
            'revision': BACKEND_REVISION, 'archive_sha256': BACKEND_SHA256}) + '\n')
        previous = target.exists()
        if previous:
            os.replace(target, backup)
        try:
            os.replace(stage, target)
        except OSError:
            if previous:
                os.replace(backup, target)
            raise
    return target


def viewer_assets_path():
    specification = importlib.util.find_spec('align')
    if specification is None or specification.origin is None:
        raise ValueError('Run this helper using the managed ALIGN environment Python.')
    return Path(specification.origin).parent / 'gui' / 'assets'


def provision_viewer_assets(target=None, *, check=False):
    target = Path(target) if target is not None else viewer_assets_path()
    if _complete(target, ASSET_FILES):
        return target
    if check:
        raise ValueError('Managed ALIGN browser assets are missing or changed; rerun ROAR setup.')
    if target.is_symlink():
        raise ValueError('Refusing to write through an ALIGN assets directory symlink: ' + str(target))
    pending = {name: _download(ASSETS_URL + name, expected)
               for name, expected in ASSET_FILES.items() if not _matches(target / name, expected)}
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.roar-assets-', dir=target) as work:
        for name, data in pending.items():
            stage = Path(work) / name
            stage.write_bytes(data)
            os.replace(stage, target / name)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend-dir', required=True, type=Path)
    parser.add_argument('--check', action='store_true', help='Verify files without downloading or changing anything')
    arguments = parser.parse_args(argv)
    try:
        backend = provision_backend(arguments.backend_dir, check=arguments.check)
        assets = provision_viewer_assets(check=arguments.check)
    except (OSError, ValueError, KeyError, tarfile.TarError, urllib.error.URLError) as error:
        print('ALIGN setup: ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps({'ready': True, 'backend_path': str(backend),
                      'backend_revision': BACKEND_REVISION, 'viewer_assets': str(assets)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
