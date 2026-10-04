import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.test_bundle_reader import _bundle_bytes
from w3sub_app import dialogue_index as module
from w3sub_app.models import GameInstallation, GameVersion, Storefront
from w3sub_app.wolvenkit_scene_refs import HelperIdentity, LocalizedReference, SceneReferenceError


class DialogueBundleIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'game'
        self.bundle = self.root / 'content' / 'content0' / 'a.bundle'
        self.bundle.parent.mkdir(parents=True)
        self.state = Path(self.temporary.name) / 'state'
        self.game = GameInstallation(self.root, Storefront.UNKNOWN, GameVersion('5.0.0.1', None), {})
        self.helper = HelperIdentity('helper-1', 'commit', 'a' * 64, True)
        self.addCleanup(patch.stopall)
        patch.object(module, 'state_root_for', return_value=self.state, create=True).start()
        patch.object(module, 'validate_wolvenkit_helper', return_value=self.helper, create=True).start()
        patch('w3sub_app.wolvenkit_scene_refs.validate_wolvenkit_helper', return_value=self.helper).start()
        self.scan = patch.object(module, 'scan_localized_references', side_effect=self.references, create=True).start()

    def write_bundle(self, rows=None, path=None):
        path = path or self.bundle
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_bundle_bytes(rows or [('line.w2scene', b'CR2Wline', 1)]))

    def references(self, manifest_path, helper_path, work_dir, progress_callback=None, *, session=None):
        result = []
        resources = json.loads(Path(manifest_path).read_text())['resources']
        for resource in resources:
            marker = Path(resource['path']).read_bytes()[4:]
            kind = {
                b'line': ('CStorySceneLine', 'dialogLine'),
                b'choice': ('CStorySceneChoiceLine', 'choiceLine'),
                b'item': ('CItem', 'displayName'),
            }.get(marker)
            if kind:
                result.append(LocalizedReference(100, resource['resource_identity'], *kind))
        return tuple(result)

    def test_dialogline_is_spoken_and_choiceline_is_not(self):
        self.write_bundle()
        spoken = module.build_dialogue_index(self.game)
        self.assertIsNotNone(spoken)
        self.assertEqual(spoken.context_for_id('100'), module.DialogContext.SCENE_SUBTITLE)
        self.assertEqual(spoken.context_for('100', 'abcd'), module.DialogContext.SCENE_SUBTITLE)
        self.write_bundle([('choice.w2scene', b'CR2Wchoice', 0)])
        choice = module.build_dialogue_index(self.game)
        self.assertEqual(choice.context_for_id('100'), module.DialogContext.CHOICE_UI)

    def test_other_resource_reuse_makes_dialogue_id_ambiguous(self):
        self.write_bundle([('line.w2scene', b'CR2Wline', 0), ('item.w2ent', b'CR2Witem', 1)])
        index = module.build_dialogue_index(self.game)
        self.assertIsNotNone(index)
        self.assertEqual(index.context_for_id('100'), module.DialogContext.AMBIGUOUS)

    def test_duplicate_physical_entries_union_contexts(self):
        self.write_bundle([('same.w2scene', b'CR2Wline', 0), ('same.w2scene', b'CR2Wchoice', 0)])
        index = module.build_dialogue_index(self.game)
        self.assertIsNotNone(index)
        self.assertEqual(index.context_for_id('100'), module.DialogContext.AMBIGUOUS)
        self.assertEqual(len(index.physical_inventory), 2)

    def test_incomplete_or_unparsed_resource_never_publishes_index(self):
        self.write_bundle()
        self.assertIsNotNone(module.build_dialogue_index(self.game))
        self.scan.side_effect = SceneReferenceError('line.w2scene: parse failed')
        self.assertIsNone(module.build_dialogue_index(self.game))
        self.assertIsNone(module.load_dialogue_index(self.game))
        self.assertFalse((self.state / 'dialogue-index.json').exists())
        self.scan.side_effect = self.references
        self.write_bundle([('line.w2scene', b'CR2Wline', 0), ('unknown.buffer', b'unknown format', 0)])
        self.assertIsNone(module.build_dialogue_index(self.game))
        self.assertIn('unknown.buffer', module.dialogue_index_unavailable_reason(self.game))

    def test_cached_index_binds_version_inventory_payloads_and_helper(self):
        self.write_bundle()
        index = module.build_dialogue_index(self.game)
        self.assertIsNotNone(index)
        self.scan.reset_mock()
        loaded = module.load_dialogue_index(self.game)
        self.assertEqual(loaded.digest, index.digest)
        self.scan.assert_not_called()
        # Preserve supported revision compatibility, but reject a major/minor change.
        newer = replace(self.game, version=GameVersion('5.0.0.2', 'new-build'))
        self.assertIsNotNone(module.load_dialogue_index(newer))
        unsupported = replace(self.game, version=GameVersion('5.1.0.1', None))
        self.assertIsNone(module.load_dialogue_index(unsupported))
        with patch.object(module, 'validate_wolvenkit_helper', return_value=replace(self.helper, executable_sha256='b' * 64)):
            self.assertIsNone(module.load_dialogue_index(self.game))
        # Even unchanged metadata must not conceal changed contributing bytes.
        with patch.object(module, 'read_bundle_entry', return_value=b'CR2Witem', create=True):
            self.assertIsNone(module.load_dialogue_index(self.game))
        self.write_bundle([('other.w2scene', b'CR2Wline', 0)], self.root / 'dlc' / 'ep1' / 'b.bundle')
        self.assertIsNone(module.load_dialogue_index(self.game))

    def test_schema_one_cache_is_not_loaded_as_complete(self):
        self.write_bundle()
        self.state.mkdir()
        (self.state / 'dialogue-index.json').write_text(json.dumps({'schema_version': 1}))
        self.assertIsNone(module.load_dialogue_index(self.game))

    def test_corrupt_cache_is_unavailable(self):
        self.write_bundle()
        self.state.mkdir()
        cache = self.state / 'dialogue-index.json'
        cache.write_text('not JSON')
        self.assertIsNone(module.load_dialogue_index(self.game))
        self.assertIsNotNone(module.build_dialogue_index(self.game))
        payload = json.loads(cache.read_text())
        payload['digest'] = 'bad'
        cache.write_text(json.dumps(payload))
        self.assertIsNone(module.load_dialogue_index(self.game))

    def test_malformed_cache_inventory_is_unavailable(self):
        self.write_bundle()
        self.assertIsNotNone(module.build_dialogue_index(self.game))
        cache = self.state / 'dialogue-index.json'
        payload = json.loads(cache.read_text())
        payload['bundle_scan']['bundle_inventory'] = [[]]
        cache.write_text(json.dumps(payload))
        self.assertIsNone(module.load_dialogue_index(self.game))

    def test_loose_resource_and_format_distribution_are_reported(self):
        self.write_bundle()
        (self.bundle.parent / 'unparsed.ws').write_bytes(b'unknown script')
        self.assertIsNone(module.build_dialogue_index(self.game))
        reason = module.dialogue_index_unavailable_reason(self.game)
        self.assertIn('unparsed.ws', reason)
        status = json.loads((self.state / 'dialogue-index-status.json').read_text())
        self.assertEqual(status['counts']['loose_formats'], {'.ws': 1})
        self.scan.assert_not_called()

    def test_dlc_union_and_tombstone_exclusion(self):
        self.write_bundle()
        self.write_bundle([('choice.w2scene', b'CR2Wchoice', 0)], self.root / 'dlc' / 'ep1' / 'b.bundle')
        tombstone = self.root / 'dlc' / 'dlc-tombstones' / 'bad.bundle'
        tombstone.parent.mkdir(parents=True)
        tombstone.write_bytes(b'not a bundle')
        index = module.build_dialogue_index(self.game)
        self.assertEqual(index.context_for_id('100'), module.DialogContext.AMBIGUOUS)
        self.assertEqual(len(index.bundle_inventory), 2)
        self.assertIsNotNone(module.load_dialogue_index(self.game))

    def test_zero_reference_resources_are_hashed_and_temporary_payloads_removed(self):
        self.write_bundle([('line.w2scene', b'CR2Wline', 0), ('unused.w2ent', b'CR2Wnone', 0)])
        index = module.build_dialogue_index(self.game)
        self.assertEqual(len(index.source_fingerprint.entries), 2)
        self.assertEqual(sorted(path.name for path in self.state.iterdir()), ['dialogue-index.json'])
        self.scan.side_effect = SceneReferenceError('line.w2scene: failure')
        self.assertIsNone(module.build_dialogue_index(self.game))
        self.assertEqual(sorted(path.name for path in self.state.iterdir()), ['dialogue-index-status.json'])

    def test_cache_freshness_validates_package_without_process_probe(self):
        self.write_bundle()
        self.assertIsNotNone(module.build_dialogue_index(self.game))
        with patch.object(module, 'validate_wolvenkit_helper', return_value=self.helper) as validate:
            self.assertIsNotNone(module.load_dialogue_index(self.game))
            validate.assert_called_once_with(module.DEFAULT_HELPER_PATH, check_protocol=False)

    def test_generation_load_checks_resource_freshness_only_once(self):
        from w3sub_app import generation
        self.write_bundle()
        self.assertIsNotNone(module.build_dialogue_index(self.game))
        reads = []
        original_read = module.read_bundle_entry
        def count_read(*arguments):
            reads.append(arguments[1].depot_path)
            return original_read(*arguments)
        with patch.object(module, 'read_bundle_entry', side_effect=count_read):
            index = generation._current_dialogue_index(self.game)
        self.assertIsNotNone(index)
        self.assertEqual(reads, ['line.w2scene'])
        self.write_bundle([('line.w2scene', b'CR2Wchoice', 0)])
        self.assertIsNone(generation._current_dialogue_index(self.game))

    def test_build_reuses_validated_session_but_revalidates_before_publication(self):
        self.write_bundle([('line.w2scene', b'CR2Wline', 0), ('item.w2ent', b'CR2Witem', 0)])
        from w3sub_app import wolvenkit_scene_refs as refs
        with patch.object(module, '_BATCH_SIZE', 1), \
             patch.object(refs, 'validate_wolvenkit_helper', return_value=self.helper) as begin, \
             patch.object(module, 'validate_wolvenkit_helper', return_value=self.helper) as finish:
            index = module.build_dialogue_index(self.game)
        self.assertIsNotNone(index)
        begin.assert_called_once_with(module.DEFAULT_HELPER_PATH)
        finish.assert_called_once_with(module.DEFAULT_HELPER_PATH, check_protocol=False)
        self.assertEqual(index.context_for_id('100'), module.DialogContext.AMBIGUOUS)
        sessions = [call.kwargs['session'] for call in self.scan.call_args_list]
        self.assertEqual(len(sessions), 2)
        self.assertIs(sessions[0], sessions[1])

    def test_changed_helper_during_scan_cannot_publish_index(self):
        self.write_bundle()
        from w3sub_app import wolvenkit_scene_refs as refs
        with patch.object(refs, 'validate_wolvenkit_helper', return_value=self.helper), \
             patch.object(module, 'validate_wolvenkit_helper', return_value=replace(
                 self.helper, executable_sha256='b' * 64)):
            self.assertIsNone(module.build_dialogue_index(self.game))
        self.assertFalse((self.state / 'dialogue-index.json').exists())

    def test_changed_game_during_scan_is_not_published(self):
        self.write_bundle()
        def changed_game(*arguments, **keywords):
            references = self.references(*arguments, **keywords)
            self.write_bundle([('choice.w2scene', b'CR2Wchoice', 0)])
            return references
        self.scan.side_effect = changed_game
        self.assertIsNone(module.build_dialogue_index(self.game))
        self.assertFalse((self.state / 'dialogue-index.json').exists())

    def test_id_context_unions_distinct_localization_keys(self):
        # Existing parser integrations retain their keyed input boundary, but
        # separate keys cannot conceal reuse of the same numeric ID.
        path = self.root / 'content' / 'fixture.json'
        path.write_text('{}')
        index = module.DialogueIndex.from_validated_references(
            self.game, [path], {('100', 'a'): [module.DialogContext.SCENE_SUBTITLE],
                                ('100', 'b'): [module.DialogContext.OTHER]},
            source_roots=[path.parent], source_patterns=['*.json'])
        self.assertEqual(index.context_for('100', 'a'), module.DialogContext.AMBIGUOUS)
        self.assertEqual(index.context_for_id('100'), module.DialogContext.AMBIGUOUS)
    def test_unavailable_reason_includes_failed_resource(self):
        self.write_bundle()
        self.scan.side_effect = SceneReferenceError('content/content0/a.bundle#0:line.w2scene: unparsed variable')
        self.assertIsNone(module.build_dialogue_index(self.game))
        reason = module.dialogue_index_unavailable_reason(self.game)
        self.assertIn('line.w2scene', reason)
        self.assertIn('unparsed variable', reason)
        self.assertIn('Full-text merge remains available', reason)

    def test_malformed_typed_reference_is_rejected_at_builder_boundary(self):
        self.write_bundle()
        self.scan.side_effect = lambda manifest, *args, **kwargs: (LocalizedReference(True, json.loads(manifest.read_text())['resources'][0]['resource_identity'], 'CStorySceneLine', 'dialogLine'),)
        self.assertIsNone(module.build_dialogue_index(self.game))

    def test_empty_missing_or_unreadable_inventory_is_unavailable(self):
        self.assertIsNone(module.build_dialogue_index(self.game))
        self.write_bundle()
        self.assertIsNotNone(module.build_dialogue_index(self.game))
        self.bundle.unlink()
        self.assertIsNone(module.load_dialogue_index(self.game))


if __name__ == '__main__':
    unittest.main()
