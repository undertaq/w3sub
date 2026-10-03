import json
from pathlib import Path
import tempfile
import unittest

from w3sub_app.models import Storefront
from w3sub_app.storefronts import StoreRoots, discover_candidates


class FixtureRegistry:
    def __init__(self):
        self.records = {}

    def add(self, hive, view, key, **values):
        self.records[hive, view, key] = values

    def values(self, hive, view, key):
        return self.records.get((hive, view, key), {})

    def subkeys(self, hive, view, key):
        prefix = key + "\\"
        return sorted({k[len(prefix):].split("\\")[0]
                       for h, v, k in self.records
                       if h == hive and v == view and k.startswith(prefix)})


class StorefrontTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.registry = FixtureRegistry()
        self.roots = StoreRoots()

    def game(self, name="game", executable="x64"):
        root = self.base / name
        (root / "content").mkdir(parents=True)
        (root / "dlc").mkdir()
        exe = root / "bin" / executable / "witcher3.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"fixture executable")
        return root

    def steam(self, library, game, build="12345"):
        apps = library / "steamapps"
        apps.mkdir(parents=True, exist_ok=True)
        (apps / "appmanifest_292030.acf").write_text(
            '"AppState" { "appid" "292030" "installdir" "' + game.name
            + '" "buildid" "' + build + '" }', encoding="utf-8")

    def discover(self):
        return discover_candidates(self.registry, self.roots)

    def test_steam_registry_library_manifest_build(self):
        client = self.base / "client"
        library = self.base / "Library"
        game = self.game("Library/steamapps/common/The Witcher 3")
        self.steam(library, game)
        (client / "steamapps").mkdir(parents=True)
        path = str(library).replace("\\", "\\\\")
        (client / "steamapps/libraryfolders.vdf").write_text(
            '"libraryfolders" { "0" { "path" "' + path + '" "apps" { "292030" "1" } } }')
        self.registry.add("HKCU", 64, r"Software\Valve\Steam", SteamPath=str(client))
        candidates = self.discover()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].root, game.resolve())
        self.assertEqual(candidates[0].storefront, Storefront.STEAM)
        self.assertEqual(candidates[0].store_build_id, "12345")
        self.assertIn("appmanifest_292030.acf", candidates[0].record_source)

    def test_legacy_steam_library_and_injected_root(self):
        client = self.base / "client"
        library = self.base / "legacy"
        game = self.game("legacy/steamapps/common/The Witcher 3", "x64_dx12")
        self.steam(library, game)
        (client / "steamapps").mkdir(parents=True)
        (client / "steamapps/libraryfolders.vdf").write_text(
            '"LibraryFolders" { "1" "' + str(library).replace("\\", "\\\\") + '" }')
        self.roots = StoreRoots(steam_roots=(client,))
        self.assertEqual(self.discover()[0].root, game.resolve())

    def test_gog_game_and_uninstall_both_views_deduplicate_provenance(self):
        game = self.game()
        for hive, view in (("HKLM", 32), ("HKCU", 64)):
            self.registry.add(hive, view, r"Software\GOG.com\Games\1207664643",
                              gameName="The Witcher 3: Wild Hunt", path=str(game), version="5.00")
            self.registry.add(hive, view,
                              r"Software\Microsoft\Windows\CurrentVersion\Uninstall\witcher",
                              DisplayName="The Witcher 3: Wild Hunt - GOTY", Publisher="GOG.com",
                              InstallLocation=str(game / "."))
        candidate, = self.discover()
        self.assertEqual(candidate.storefront, Storefront.GOG)
        self.assertEqual(candidate.store_build_id, "5.00")
        sources = candidate.record_source.split("; ")
        self.assertEqual(len(sources), 4)
        self.assertEqual(len(set(sources)), 4)

    def test_epic_override_and_normal_manifest_keep_build(self):
        game = self.game()
        manifests = self.base / "normal"
        manifests.mkdir()
        (manifests / "witcher.item").write_text(json.dumps({
            "DisplayName": "The Witcher 3: Wild Hunt", "InstallLocation": str(game), "AppVersion": "5.00-epic"}))
        self.registry.add("HKLM", 32, r"Software\Epic Games\The Witcher 3",
                          InstallLocation=str(game))
        self.roots = StoreRoots(epic_manifest_roots=(manifests,))
        candidate, = self.discover()
        self.assertEqual(candidate.storefront, Storefront.EPIC)
        self.assertEqual(candidate.store_build_id, "5.00-epic")
        self.assertEqual(len(candidate.record_source.split("; ")), 2)

    def test_epic_launcher_relative_manifests_and_eos_rejected(self):
        game = self.game()
        launcher = self.base / "launcher"
        manifests = launcher / "Data/Manifests"
        manifests.mkdir(parents=True)
        (manifests / "game.item").write_text(json.dumps({
            "DisplayName": "The Witcher 3 Wild Hunt", "InstallLocation": str(game)}))
        self.registry.add("HKCU", 64, r"Software\Epic Games\EpicGamesLauncher",
                          AppDataPath=str(launcher))
        self.registry.add("HKLM", 64, r"Software\Epic Games\Epic Online Services",
                          InstallLocation=str(self.game("EOS")))
        self.assertEqual([c.root for c in self.discover()], [game.resolve()])

    def test_stale_incomplete_unrelated_and_malformed_records_rejected(self):
        manifests = self.base / "manifests"
        manifests.mkdir()
        (manifests / "bad.item").write_text("{broken")
        (manifests / "array.item").write_text("[]")
        unrelated = self.game("other")
        (manifests / "other.item").write_text(json.dumps({
            "DisplayName": "Other Game", "InstallLocation": str(unrelated)}))
        self.roots = StoreRoots(epic_manifest_roots=(manifests,))
        for index, root in enumerate((self.base / "missing", self.base / "incomplete")):
            root.mkdir()
            self.registry.add("HKLM", 64, rf"Software\GOG.com\Games\{index}",
                              gameName="The Witcher 3", path=str(root))
        self.assertEqual(self.discover(), [])

    def test_steam_manifest_cannot_escape_common_directory(self):
        library = self.base / "library"
        game = self.game("escape")
        self.steam(library, game)
        (library / "steamapps/appmanifest_292030.acf").write_text(
            '"AppState" { "appid" "292030" "installdir" "../../escape" }')
        self.roots = StoreRoots(steam_roots=(library,))
        self.assertEqual(self.discover(), [])

    def test_steam_empty_metadata_values_preserve_record_pairs(self):
        library = self.base / "library"
        game = self.game("library/steamapps/common/The Witcher 3")
        self.steam(library, game)
        (library / "steamapps/appmanifest_292030.acf").write_text(
            '"AppState" { "appid" "292030" "UserConfig" { "BetaKey" "" } '
            '"installdir" "The Witcher 3" "buildid" "777" }')
        self.roots = StoreRoots(steam_roots=(library,))
        candidate, = self.discover()
        self.assertEqual(candidate.root, game.resolve())
        self.assertEqual(candidate.store_build_id, "777")

    def test_non_gog_uninstall_record_is_not_labeled_gog(self):
        game = self.game()
        self.registry.add("HKLM", 64,
                          r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Steam App 292030",
                          DisplayName="The Witcher 3", Publisher="CD PROJEKT RED",
                          InstallLocation=str(game))
        self.assertEqual(self.discover(), [])

    def test_remastered_layout_without_dlc_is_a_valid_candidate(self):
        game = self.game("library/steamapps/common/The Witcher 3", "x64_dx12")
        (game / "dlc").rmdir()
        (game / "dlc-tombstones").mkdir()
        library = self.base / "library"
        self.steam(library, game)
        self.roots = StoreRoots(steam_roots=(library,))
        candidate, = self.discover()
        self.assertEqual(candidate.root, game.resolve())


if __name__ == "__main__":
    unittest.main()
