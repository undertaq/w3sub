import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from w3sub_app import config
from w3sub_app.models import AppConfig


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="w3sub config ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def use_state_root(self):
        patcher = patch.dict(os.environ, {"LOCALAPPDATA": str(self.base)}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_game(self, name="game"):
        root = self.base / name
        (root / "content").mkdir(parents=True)
        executable = root / "bin" / "x64" / "witcher3.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"fixture")
        return root

    def test_missing_or_invalid_json_uses_safe_defaults(self):
        self.use_state_root()
        self.assertEqual(config.load_config(), AppConfig())
        path = config.app_state_root() / "config.json"
        path.parent.mkdir(parents=True)
        path.write_text("{bad", encoding="utf-8")
        self.assertEqual(config.load_config(), AppConfig())

    def test_load_config_keeps_only_existing_game_and_converter_paths(self):
        self.use_state_root()
        game = self.make_game()
        converter = self.base / "compatible.exe"
        converter.write_bytes(b"converter")
        config.app_state_root().mkdir(parents=True)
        config_file = config.app_state_root() / "config.json"
        config_file.write_text(json.dumps({
            "last_game_root": str(game),
            "converter_path": str(converter),
            "account": "must not be retained",
        }), encoding="utf-8")
        self.assertEqual(config.load_config(), AppConfig(game.resolve(), converter.resolve()))

        config_file.write_text(json.dumps({
            "last_game_root": str(self.base / "missing"),
            "converter_path": str(self.base / "missing.exe"),
        }), encoding="utf-8")
        self.assertEqual(config.load_config(), AppConfig())

    def test_state_root_is_stable_for_normalized_game_path_and_isolated_per_game(self):
        self.use_state_root()
        game = self.make_game()
        equivalent = game / "."
        first = config.state_root_for(game)
        self.assertEqual(first, config.state_root_for(equivalent))
        other = self.make_game("other-game")
        second = config.state_root_for(other)
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, config.app_state_root() / "games")
        self.assertNotEqual(first, game / "state")

    def test_save_config_persists_paths_atomically_without_account_data(self):
        self.use_state_root()
        game = self.make_game()
        converter = self.base / "compatible.exe"
        converter.write_bytes(b"converter")
        config.save_config(AppConfig(game, converter))
        config_file = config.app_state_root() / "config.json"
        saved = json.loads(config_file.read_text(encoding="utf-8"))
        self.assertEqual(saved, {
            "converter_path": str(converter.resolve()),
            "last_game_root": str(game.resolve()),
        })
        self.assertEqual(config.load_config(), AppConfig(game.resolve(), converter.resolve()))
        self.assertEqual(list(config.app_state_root().glob("*.tmp")), [])

    def test_invalid_paths_cannot_be_saved_as_usable_selections(self):
        self.use_state_root()
        config.save_config(AppConfig(self.base / "not a game", self.base / "missing.exe"))
        self.assertEqual(config.load_config(), AppConfig())


if __name__ == "__main__":
    unittest.main()
