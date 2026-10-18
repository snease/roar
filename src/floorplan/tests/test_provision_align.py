"""Pinned runtime data installs safely and checks without network or writes."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
specification = importlib.util.spec_from_file_location('provision_align', ROOT / 'bin/provision_align.py')
provision = importlib.util.module_from_spec(specification)
specification.loader.exec_module(provision)


class ProvisionAlignTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.target = self.root / 'runtime' / 'SKY130_PDK'
        self.files = {'mos.py': b'pinned backend source\n', 'LICENSE': b'upstream license\n'}
        self.hashes = {name: hashlib.sha256(data).hexdigest() for name, data in self.files.items()}

    def archive(self, *, bad=False, link=False):
        stream = io.BytesIO()
        prefix = 'ALIGN-pdk-sky130-' + provision.BACKEND_REVISION + '/'
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            for name, data in self.files.items():
                path = prefix + (name if name == 'LICENSE' else 'SKY130_PDK/' + name)
                member = tarfile.TarInfo(path)
                if name == 'mos.py' and link:
                    member.type, member.linkname = tarfile.SYMTYPE, '/tmp/unrelated'
                    archive.addfile(member)
                else:
                    data = b'tampered' if bad and name == 'mos.py' else data
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
            for name in ('../../outside', prefix + 'SKY130_PDK/__pycache__/old.pyc'):
                member = tarfile.TarInfo(name)
                member.size = 4
                archive.addfile(member, io.BytesIO(b'skip'))
        return stream.getvalue()

    def install(self, payload=None):
        with patch.object(provision, 'BACKEND_FILES', self.hashes), patch.object(provision, '_download', return_value=payload or self.archive()):
            return provision.provision_backend(self.target)

    def test_backend_pins_match_the_runtime_profile(self):
        profile = json.loads((ROOT / 'src/floorplan/align_profile.json').read_text())
        self.assertEqual(provision.BACKEND_REVISION, profile['backend_revision'])
        for name, digest in profile['backend_hashes'].items():
            self.assertEqual(provision.BACKEND_FILES[name], digest)
        self.assertIn('LICENSE', provision.BACKEND_FILES)

    def test_check_missing_files_does_not_create_directories_or_download(self):
        with patch.object(provision, '_download') as download:
            with self.assertRaisesRegex(ValueError, 'rerun ROAR setup'):
                provision.provision_backend(self.target, check=True)
            with self.assertRaisesRegex(ValueError, 'rerun ROAR setup'):
                provision.provision_viewer_assets(self.root / 'assets', check=True)
        download.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_installs_only_pinned_files_and_can_check_offline(self):
        self.install()
        self.assertEqual({p.name for p in self.target.iterdir()}, set(self.files) | {provision._MARKER})
        self.assertFalse((self.root / 'outside').exists())
        with patch.object(provision, 'BACKEND_FILES', self.hashes), patch.object(provision, '_download') as download:
            self.assertEqual(provision.provision_backend(self.target, check=True), self.target)
            self.assertEqual(provision.provision_backend(self.target), self.target)
        download.assert_not_called()

    def test_repair_replaces_only_a_recognized_managed_backend(self):
        self.install()
        (self.target / 'mos.py').write_text('changed')
        self.install()
        self.assertEqual((self.target / 'mos.py').read_bytes(), self.files['mos.py'])
        (self.target / provision._MARKER).unlink()
        (self.target / 'mos.py').write_text('user source')
        with self.assertRaisesRegex(ValueError, 'unrecognized backend'):
            self.install()
        self.assertEqual((self.target / 'mos.py').read_text(), 'user source')

    def test_bad_backend_payload_preserves_existing_files(self):
        self.install()
        (self.target / 'mos.py').write_text('needs repair')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.install(self.archive(bad=True))
        self.assertEqual((self.target / 'mos.py').read_text(), 'needs repair')

    def test_archive_links_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'link or special file'):
            self.install(self.archive(link=True))
        self.assertFalse(self.target.exists())

    def test_failed_atomic_replacement_restores_previous_backend(self):
        self.install()
        (self.target / 'mos.py').write_text('previous')
        replace = provision.os.replace
        def fail_stage(source, target):
            if Path(source).name == 'new':
                raise OSError('simulated filesystem error')
            return replace(source, target)
        with patch.object(provision.os, 'replace', side_effect=fail_stage):
            with self.assertRaisesRegex(OSError, 'simulated filesystem'):
                self.install()
        self.assertEqual((self.target / 'mos.py').read_text(), 'previous')

    def test_viewer_assets_are_checked_and_repaired_without_unrelated_writes(self):
        target = self.root / 'assets'
        with patch.object(provision, 'ASSET_FILES', self.hashes), patch.object(provision, '_download', side_effect=lambda url, digest: self.files[url.rsplit('/', 1)[1]]) as download:
            provision.provision_viewer_assets(target)
            self.assertEqual(download.call_count, 2)
            download.reset_mock()
            provision.provision_viewer_assets(target, check=True)
            download.assert_not_called()
            (target / 'mos.py').write_text('damaged')
            provision.provision_viewer_assets(target)
            self.assertEqual(download.call_count, 1)

    def test_download_integrity_failure_is_actionable(self):
        with patch.object(provision.urllib.request, 'urlopen', return_value=io.BytesIO(b'bad download')):
            with self.assertRaisesRegex(ValueError, 'checksum does not match'):
                provision._download('https://example.com/pinned', '0' * 64)
