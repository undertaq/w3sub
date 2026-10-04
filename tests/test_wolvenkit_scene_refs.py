import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from w3sub_app import wolvenkit_scene_refs as refs


class WolvenkitHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.helper = self.root / 'helper.exe'
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_text(json.dumps({'schema': 1, 'resources': [
            {'resource_identity': 'content/bundle:0', 'path': str(self.root / 'scene.w2scene')}
        ]}), encoding='utf-8')

    def scan(self, records, returncode=0, stderr=''):
        result = subprocess.CompletedProcess([], returncode, '\n'.join(json.dumps(r) for r in records), stderr)
        with patch.object(refs, 'validate_wolvenkit_helper', create=True), patch('subprocess.run', return_value=result):
            return refs.scan_localized_references(self.manifest, self.helper, self.root / 'work')

    def record(self, references=None, **changes):
        record = {'schema': 1, 'resource_identity': 'content/bundle:0', 'ok': True,
                  'references': references if references is not None else []}
        record.update(changes)
        return record

    def test_extracts_spoken_choice_and_other_localized_references(self):
        self.assertTrue(hasattr(refs, 'scan_localized_references'), 'batch adapter is required')
        result = self.scan([self.record([
            {'string_id': 100, 'owner_type': 'CStorySceneLine', 'field_name': 'dialogLine'},
            {'string_id': 101, 'owner_type': 'CStorySceneChoiceLine', 'field_name': 'choiceLine'},
            {'string_id': 102, 'owner_type': 'CItem', 'field_name': 'displayName'},
        ])])
        self.assertEqual([(r.string_id, r.owner_type, r.field_name, r.resource_identity) for r in result], [
            (100, 'CStorySceneLine', 'dialogLine', 'content/bundle:0'),
            (101, 'CStorySceneChoiceLine', 'choiceLine', 'content/bundle:0'),
            (102, 'CItem', 'displayName', 'content/bundle:0'),
        ])

    def test_rejects_malformed_or_unrecognized_reference_records(self):
        self.assertTrue(hasattr(refs, 'scan_localized_references'), 'batch adapter is required')
        for bad in [True, -1, 0x100000000, '100', None]:
            with self.subTest(bad=bad), self.assertRaises(refs.SceneReferenceError):
                self.scan([self.record([{'string_id': bad, 'owner_type': 'CItem', 'field_name': 'name'}])])
        for changes in [{'schema': 99}, {'schema': True}, {'references': [{}]}, {'references': [{'string_id': 1, 'owner_type': '', 'field_name': 'name'}]}]:
            with self.subTest(changes=changes), self.assertRaises(refs.SceneReferenceError):
                self.scan([self.record(**changes)])

    def test_requires_one_result_for_every_manifest_resource(self):
        self.assertTrue(hasattr(refs, 'scan_localized_references'), 'batch adapter is required')
        for records in [[], [self.record(), self.record()], [self.record(resource_identity='unknown')]]:
            with self.subTest(records=records), self.assertRaises(refs.SceneReferenceError):
                self.scan(records)

    def test_helper_failure_includes_resource_diagnostic(self):
        self.assertTrue(hasattr(refs, 'scan_localized_references'), 'batch adapter is required')
        with self.assertRaisesRegex(refs.SceneReferenceError, 'content/bundle:0.*unsupported'):
            self.scan([self.record(ok=False, error='unsupported chunk')], 1)

    def test_timeout_is_an_index_unavailable_error(self):
        self.assertTrue(hasattr(refs, 'scan_localized_references'), 'batch adapter is required')
        with patch.object(refs, 'validate_wolvenkit_helper', create=True), patch('subprocess.run', side_effect=subprocess.TimeoutExpired('helper', 10)):
            with self.assertRaises(refs.SceneReferenceError):
                refs.scan_localized_references(self.manifest, self.helper, self.root / 'work')

    def test_missing_helper_fails_closed(self):
        self.assertTrue(hasattr(refs, 'validate_wolvenkit_helper'), 'identity validation is required')
        with self.assertRaises(refs.SceneReferenceError):
            refs.validate_wolvenkit_helper(self.helper)


class HelperIdentityTests(unittest.TestCase):
    def setUp(self):
        import hashlib
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.helper = self.bin / 'WolvenKit.CLI.exe'
        self.helper.write_bytes(b'fixture helper')
        (self.bin / 'parser.dll').write_bytes(b'fixture parser')
        (self.root / 'source.zip').write_bytes(b'fixture source')
        self.metadata = {'schema': 1, 'version': refs.HELPER_VERSION,
                         'upstream_commit': refs.UPSTREAM_COMMIT, 'supports_v164': True,
                         'source_packages': ['source.zip'], 'files': {}}
        for p in [self.helper, self.bin / 'parser.dll', self.root / 'source.zip']:
            self.metadata['files'][p.relative_to(self.root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
        self.manifest = self.root / 'helper-manifest.json'
        self.manifest.write_text(json.dumps(self.metadata), encoding='utf-8')
        self.manifest_hash = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        self.identity = subprocess.CompletedProcess([], 0, json.dumps({
            'schema': 1, 'version': refs.HELPER_VERSION, 'upstream_commit': refs.UPSTREAM_COMMIT,
            'supports_v164': True}), '')

    def validate(self):
        with patch.object(refs, 'TRUSTED_MANIFEST_SHA256', self.manifest_hash, create=True), patch('subprocess.run', return_value=self.identity):
            return refs.validate_wolvenkit_helper(self.helper)

    def test_validates_complete_pinned_distribution(self):
        identity = self.validate()
        self.assertTrue(identity.supports_v164)
        self.assertEqual(identity.upstream_commit, refs.UPSTREAM_COMMIT)

    def test_session_hashes_distribution_once_across_multiple_batches(self):
        manifest = self.root / 'resources.json'
        manifest.write_text(json.dumps({'schema': 1, 'resources': [
            {'resource_identity': 'line', 'path': str(self.root / 'scene.cr2w')}
        ]}), encoding='utf-8')
        batch = subprocess.CompletedProcess([], 0, json.dumps({
            'schema': 1, 'resource_identity': 'line', 'ok': True, 'references': [
                {'string_id': 100, 'owner_type': 'CStorySceneLine', 'field_name': 'dialogLine'}
            ]}), '')
        hashed = []
        original_hash = refs._sha256
        def count_hash(path):
            hashed.append(path.name)
            return original_hash(path)
        with patch.object(refs, 'TRUSTED_MANIFEST_SHA256', self.manifest_hash), \
             patch.object(refs, '_sha256', side_effect=count_hash), \
             patch('subprocess.run', side_effect=[self.identity, batch, batch, batch]):
            self.assertTrue(hasattr(refs, 'WolvenkitHelperSession'), 'a validated batch session is required')
            session = refs.WolvenkitHelperSession(self.helper)
            for _ in range(3):
                result = refs.scan_localized_references(manifest, self.helper, self.root / 'work',
                                                         session=session)
                self.assertEqual([(r.string_id, r.field_name) for r in result], [(100, 'dialogLine')])
        self.assertEqual(hashed.count('source.zip'), 1)
        self.assertEqual(hashed.count('parser.dll'), 1)

    def test_rejects_modified_manifest_even_if_it_claims_new_hashes(self):
        import hashlib
        self.helper.write_bytes(b'replacement helper')
        self.metadata['files']['bin/WolvenKit.CLI.exe'] = hashlib.sha256(self.helper.read_bytes()).hexdigest()
        self.manifest.write_text(json.dumps(self.metadata), encoding='utf-8')
        with self.assertRaisesRegex(refs.SceneReferenceError, 'manifest.*modified|modified.*manifest'):
            self.validate()

    def test_rejects_modified_runtime_or_missing_source(self):
        (self.bin / 'parser.dll').write_bytes(b'modified')
        with self.assertRaisesRegex(refs.SceneReferenceError, 'parser.dll'):
            self.validate()
        (self.bin / 'parser.dll').write_bytes(b'fixture parser')
        (self.root / 'source.zip').unlink()
        with self.assertRaisesRegex(refs.SceneReferenceError, 'source.zip'):
            self.validate()

    def test_rejects_unrecognized_assembly(self):
        (self.bin / 'extra.dll').write_bytes(b'assembly')
        with self.assertRaises(refs.SceneReferenceError):
            self.validate()


if __name__ == '__main__':
    unittest.main()
