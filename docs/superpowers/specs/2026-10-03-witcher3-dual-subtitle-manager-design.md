# The Witcher 3 Dual Subtitle Manager Design

Date: 2026-10-03

## Goal

Turn the existing Witcher 3 subtitle composer into a Windows desktop tool that
can generate, install, modify, and uninstall a dual-language text update for
The Witcher 3: Wild Hunt — Remastered 5.00. A user chooses a primary language
(the game's selected language) and a secondary language, and sees the secondary
text alongside the primary text in game.

The known target installation is
`F:\Game\SteamLibrary\steamapps\common\The Witcher 3`. Local inspection found
Steam App ID 292030, a Remastered app manifest, an executable reporting
`5.0.0.1044392`, and 18 language codes represented in `.w3strings` files:
`ar`, `br`, `cn`, `cz`, `de`, `en`, `es`, `esmx`, `fr`, `hu`, `it`, `jp`,
`kr`, `pl`, `ru`, `tr`, `ua`, and `zh`. These are discovery results, not a
hard-coded promise that all future installations contain the same set.

## Existing program

The tracked implementation is `w3sub.py`. It calls the bundled
`w3strings.exe` v0.4.1, combines hard-coded `zh` and `en` resources, assumes an
obsolete `I:` drive path, and copies outputs into a relative `install/`
directory. Its install marker does not record originals or changed-file hashes,
so it cannot safely modify or uninstall a prior installation. The repository
also contains untracked generated data in `backup/`, `working/`, and `install/`
and several archives; the new application must not treat these as its managed
state or overwrite them.

## User flow

1. Launch the desktop application.
2. Choose or browse to the game root. Default the field to the known F: path
   when it exists, otherwise ask the user to browse.
3. Scan the installation. Show a clear result for supported version, detected
   language codes, converter availability, and writable game/state locations.
4. Select distinct primary and secondary languages from detected resources.
   Select either full-text combination or the existing dialogue-only mode.
5. Generate to a staging directory and show progress and actionable errors.
6. Preview a concise summary of changed resource files and confirm install or
   modify.
7. Show the installed pair, game path, backup location, and available
   Modify/Uninstall actions when the managed update is active.

The game folder remains selectable so users can relocate their Steam library.
The first supported target is the verified Remastered 5.00 Windows layout.

## Approaches considered

### Recommended: Tkinter UI with a separate Python core

Keep Python and the existing converter. Put GUI, game discovery, merge logic,
converter invocation, and installation state in focused modules. Use Tkinter
from the Python standard library for a desktop window, keeping new runtime
dependencies out of the project. This minimizes compatibility and packaging
work while replacing the fragile script flow with testable boundaries.

### PySide desktop UI

Use the same core and workflows with a richer native-feeling interface.
This would add a third-party runtime dependency and packaging work that does
not materially improve the small scan/generate/install workflow, so it is not
recommended for the first version.

## Architecture

- `game_discovery`: validate the selected game root, identify the Remastered
  5.00 executable/layout, discover `.w3strings` language codes under `content`
  and `dlc`, and inventory corresponding resource paths.
- `converter`: invoke the bundled `w3strings.exe` with an argument list (never
  a shell command), capture output/exit status, and report decode/encode errors.
- `merger`: read the converter's CSV representation, pair records by string ID
  and key, combine primary text before secondary text, and produce a new CSV
  for the selected primary language.
- `install_manager`: stage outputs, persist original files and a manifest,
  perform atomic per-file replacement with rollback on failure, and manage
  modify/uninstall conflict checks.
- `gui`: Tkinter window, game path selection, language/mode controls, scan
  result, progress, confirmation, and managed-install actions. Long-running
  operations must not block window updates.
- `w3sub.py`: keep as the stable launch entry point and start the GUI when run.
  Move processing into the focused modules; running the entry point must never
  install or modify the game without user interaction.

The converter executable is resolved relative to the application bundle or
source directory, not the current working directory. The game installation is
never used as scratch space.

## Merge behavior

- Discover languages from actual language-named `.w3strings` assets in the
  selected installation. Only offer a language when usable resources for it
  are found; primary and secondary must differ.
- Walk matching primary and secondary resource paths in `content` and `dlc`.
- Match CSV records by both string ID and key, preserving source ordering and
  comments where the converter format supports them.
- For a matching record, write the primary string first and the secondary
  string second separated by the game's supported line break marker (`<br>`).
- Keep primary-only records unchanged so missing secondary translations do not
  erase primary text. Ignore secondary-only records because the primary
  language is the destination resource and has no corresponding primary
  translation to pair.
- The default full-text mode applies the dual-line format to all matched
  records. Dialogue-only mode preserves the existing primary-only behavior for
  short strings not ending in the legacy dialogue punctuation set, while
  dual-formatting strings ending in `.,?!]` or `…`, or longer than 32 characters.
  Isolate this classification rule in the merger and document it so it can be
  changed without affecting file processing.
- Output files replace only the selected primary language's matching assets;
  all other languages and unrelated game files remain untouched.
- Validate that the converter accepts the game's source resources before
  enabling generation. Never continue with a partial conversion set.

## Install, modify, and uninstall

Managed state and exact original backups live outside the game directory under
the current user's local application-data directory, keyed by a stable hash of
the normalized game path. A manifest records the supported game identity,
language pair, mode, each target relative path, original backup path and
SHA-256, installed file SHA-256, and tool version.

Install flow:

1. Build every replacement in staging and validate the complete set.
2. Back up every original target before changing any game file.
3. Write a prepared manifest and replace each target through a temporary file
   on the same volume, using atomic rename where supported.
4. Verify installed hashes and mark the manifest active.
5. On any failure, restore every original already replaced and report whether
   rollback completed.

Modify flow:

- Require the selected game path to match the active manifest.
- Verify each currently installed target against the manifest's installed
  hash. If any file differs, stop and explain that the game or another mod has
  changed it.
- Generate the new language pair from the saved originals, never from already
  combined files. Back up no new baseline over the original baseline.
- Apply the new set transactionally and update the manifest only after
  successful verification.

Uninstall flow:

- Verify installed hashes before restoring originals. On a mismatch, stop and
  preserve both the unexpected file and the available backup; explain the
  conflict and backup location.
- Restore exact originals transactionally, verify original hashes, then mark
  the install inactive. Remove only files created by this application.
- Keep the backup set after uninstall so it remains available for recovery.

The application must never recursively remove a game directory or infer that
an untracked file belongs to it. It must list the exact files it will change
before installation.

## Error handling and safety

- Reject paths without the expected game structure or a supported 5.00
  executable under `bin\x64` or `bin\x64_dx12`. Check the executable's reported
  major/minor version as well as the required `content` and `dlc` structure.
  Explain which check failed and let the user choose another path.
- Reject incomplete language pairs, missing converter, malformed CSV, duplicate
  records that cannot be paired unambiguously, and any failed converter exit.
- Disable install until staging and validation complete.
- Prevent simultaneous operations on the same installation.
- Report permission, disk-space, converter, backup, hash-conflict, and rollback
  outcomes in plain language, retaining diagnostic details in an app log.
- Do not install when the game is running if the targeted files are locked;
  explain that the user should close the game and retry.

## Acceptance criteria

- The app starts as a GUI and can browse to and validate the known F: Remastered
  5.00 installation.
- The language selectors are populated from discovered usable game assets,
  not a fixed `zh`/`en` pair.
- A chosen primary/secondary pair produces correctly ordered dual text in
  matching entries, retains primary-only entries, and leaves unrelated language
  and game files unchanged.
- Full-text and dialogue-only modes are selectable and reflected in generated
  output.
- Install makes a verified backup and a manifest before changing any file.
- Modify changes the selected pair using the original baseline without stacking
  text.
- Uninstall restores byte-identical originals.
- If an installed file was changed outside the app, modify/uninstall stop before
  overwriting it and identify the conflict.
- A failure during multi-file install restores files already replaced, or
  clearly reports which restoration failed and where the backups are.

## Verification approach

Before enabling game-file changes, validate the converter against the selected
installation's source resources and compare a decode/re-encode round trip where
the converter supports it. During implementation, verify the merge behavior
with representative converter CSV inputs, exercise the GUI against the known
game directory, and perform an install/modify/uninstall cycle against an
isolated copied fixture rather than the live game. Confirm backup and installed
hashes and inspect the exact target-file inventory before any live install.

## Scope and rollout

This version targets Windows and The Witcher 3: Wild Hunt — Remastered 5.00
resource layout. It does not promise support for Classic 1.32, older Next-Gen
4.x, console editions, or non-Windows platforms. The known F: install is the
initial compatibility target; scanning makes the path configurable without
assuming every drive/library has identical contents.

The implementation should first make discovery, conversion, merge, and
transaction state reliable, then attach the GUI to those operations. A later
packaging decision can bundle the Python app and converter into a distributable
Windows executable; the first implementation may be launched from the local
Python environment.
