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
The verified local layout has `content/` and `dlc-tombstones/`, but no `dlc/`
directory; the tombstone directory contains empty markers and is not a resource
root. A base game without installed DLC remains valid, so `dlc/` is optional.

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
2. Find installed game folders from Windows registry data and storefront
   metadata for Steam, GOG, and Epic. Use Steam's registry path and library
   metadata, GOG game/uninstall registry records, and Epic registry overrides
   plus the launcher's install manifests. Validate all discovered folders and
   deduplicate identical paths. If more than one valid installation is found,
   let the user select one. If discovery fails, use a previously saved valid
   folder or ask the user to browse.
3. On startup, detect the selected game's version and scan its resources.
   Repeat detection and scanning immediately after the user selects another
   game folder. Show the detected version, language codes, converter status,
   and writable game/state locations.
4. Select distinct primary and secondary languages from detected resources.
   Select full-text combination or dialogue-only mode. Enable dialogue-only
   mode only when a classifier index validated for this game version is
   available; otherwise explain why it is unavailable and leave full-text mode
   available.
5. Generate to a staging directory and show progress and actionable errors.
   Persist a generation record beside the staged output with the detected game
   version and a fingerprint of all source resources used to generate it.
6. Preview a concise summary of changed resource files and confirm install or
   modify. Before installation, re-detect the game version and source
   fingerprints; require regeneration if the staged output is stale.
7. Store the detected game version and source fingerprints with the active
   install manifest. On startup and after folder selection, compare them with
   the current game and determine whether regeneration is required.
8. Show the installed pair, game path, backup location, and available
   Modify/Uninstall actions when the managed update is active.

The game folder remains selectable so users can relocate their Steam library.
The registry and Steam library metadata are discovery inputs, not the final
authority: validate the resolved folder before accepting it. The first
supported target is the verified Remastered 5.00 Windows layout.

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
  and under `dlc` when that directory exists, and inventory corresponding
  resource paths. Do not treat `dlc-tombstones` as active resources.
- `dialogue_classifier`: load or build a versioned map from localization IDs to
  their usage context in structured game resources. Classify in-dialogue scene
  subtitles separately from overhead/oneliner text, item names, HUD/UI text,
  objectives, and other strings. Enable dialogue-only mode only after this map
  is validated for the selected game build.
- `storefront_discovery`: read registry locations and each storefront's local
  install records, return validated game-root candidates with a storefront
  label, and deduplicate candidates that resolve to the same directory.
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
- Discover storefront candidates from both registry and store-owned metadata:
  - Steam: read the client path from Steam registry values, inspect
    `steamapps/libraryfolders.vdf`, and resolve App ID 292030 through its
    `appmanifest_292030.acf`.
  - GOG: inspect GOG game registry records and Windows uninstall entries for
    The Witcher 3, checking both 32-bit and 64-bit machine and user registry
    views where available.
  - Epic: inspect Epic launcher registry data and per-game install-location
    overrides, and parse the launcher's game install manifests (normally under
    `%ProgramData%\Epic\EpicGamesLauncher\Data\Manifests`). Epic manifests
    are required because registry data may not contain each game's install
    location.
- Validate each candidate using the game's executable/version and required
  resource layout. Ignore stale entries and allow manual browsing if discovery
  does not produce a valid folder. Do not scan every drive by default.
- Walk matching primary and secondary resource paths in `content` and in `dlc`
  when present. A missing `dlc` directory does not invalidate the base game;
  `dlc-tombstones` is not an active resource directory.
- Match CSV records by both string ID and key, preserving source ordering and
  comments where the converter format supports them.
- For a matching record, write the primary string first and the secondary
  string second separated by the game's supported line break marker (`<br>`).
- Keep primary-only records unchanged so missing secondary translations do not
  erase primary text. Ignore secondary-only records because the primary
  language is the destination resource and has no corresponding primary
  translation to pair.
- The default full-text mode applies the dual-line format to all matched
  records.
- Dialogue-only mode uses a version-matched classification index derived from
  structured references in the selected game's resources. It dual-formats only
  IDs confirmed as in-dialogue scene subtitles. It leaves overhead/oneliner
  text, item names, HUD/UI text, quest objectives, and other non-dialogue
  strings in the primary language.
- If one ID is used in multiple contexts or the classifier cannot determine
  its context, leave that entry in the primary language. Do not infer context
  from punctuation, length, or wording.
- The classifier index is tied to the game version and relevant resource
  fingerprints. Rebuild or validate it after game updates. If no compatible
  index can be produced for a build, disable dialogue-only mode for that build
  and keep full-text generation available.
- The exact resource parser/index source is a compatibility requirement to
  validate against the local Remastered 5.00 installation before claiming
  dialogue-only support.
- Output files replace only the selected primary language's matching assets;
  all other languages and unrelated game files remain untouched.
- Validate that the converter accepts the game's source resources before
  enabling generation. Never continue with a partial conversion set.

## Install, modify, and uninstall

Managed state and exact original backups live outside the game directory under
the current user's local application-data directory, keyed by a stable hash of
the normalized game path. A manifest records the supported game identity,
language pair, mode, game version at generation and install, storefront
identity and build identifier when available, fingerprints of the primary and
secondary source resource inventories, converter and application versions,
classifier index version/hash when used, each target relative path, original
backup path and SHA-256, and installed file SHA-256.

Keep a separate generation record beside staged outputs so their version and
input fingerprints remain available before installation. When installing,
copy that generation snapshot into the install manifest and record the version
detected immediately before files are changed as the install version.

The game version identity includes the executable's reported file/product
version and the storefront build ID when the local store manifest provides
one. Resource
fingerprints include relative paths and SHA-256 hashes for every primary and
secondary `.w3strings` input used by the generated output, including additions
or removals from either inventory.

At startup and after a game folder is selected, compare the detected version
with the version recorded for the current generated output and active install.
If the version differs, compare the input resource inventories and hashes. If
any relevant resource was added, removed, or changed, mark the output stale,
explain that the dual subtitle files need regeneration, and disable Install or
Modify. If there is no active installation, allow the user to regenerate. If
there is an active installation and its target hashes still match the
manifest, allow uninstall first, then regenerate from the restored updated
baseline. If only the executable/build metadata changed and every relevant
language resource fingerprint is identical, report that the output remains
compatible and allow the user to proceed. If inputs cannot be read or
compared, treat the output as stale. Before install, repeat this check to catch
an update that happened while the app was open.

For an active install after a game update, first verify managed target hashes.
If the source fingerprint changed, mark the active update stale. When its
installed files still match the manifest, require uninstall/restoration before
generating against the updated baseline. If the update or another tool replaced
any managed file, mark the installation conflicted, list the affected files,
and do not overwrite them; provide the saved backup location for manual
resolution before allowing regeneration or another install.

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
  major/minor version as well as the required `content` structure; `dlc` is
  optional because the base game may not have DLC installed.
  Explain which check failed and let the user choose another path.
- If a storefront registry record or install manifest is malformed,
  inaccessible, or points to a missing game folder, show the discovery issue
  and allow manual browsing.
- If the dialogue classifier cannot be built or validated for the selected
  game version, disable dialogue-only mode with an explanation; keep
  full-text mode available.
- If the game version or source fingerprint changes between generation and
  install, stop and request regeneration before touching game files.
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
- Startup discovery covers Steam, GOG, and Epic records, resolves this
  machine's Steam App ID 292030 to the F: installation, and falls back to
  manual browsing when discovery cannot produce a valid game folder.
- The app detects game version both on startup and immediately after a folder
  is selected, and displays the result.
- The language selectors are populated from discovered usable game assets,
  not a fixed `zh`/`en` pair.
- A chosen primary/secondary pair produces correctly ordered dual text in
  matching entries, retains primary-only entries, and leaves unrelated language
  and game files unchanged.
- Full-text mode combines every matching entry. Dialogue-only mode combines
  only entries confirmed as in-dialogue scene subtitles by the validated
  classifier index; it leaves overhead/oneliner text, item names, HUD/UI text,
  objectives, and ambiguous or unknown entries in the primary language. It
  uses no punctuation/length heuristic.
- If no classifier index is available for the selected game build, the UI
  disables dialogue-only mode and explains why.
- Install makes a verified backup and a manifest before changing any file.
- Generation and install metadata include the game version, storefront and
  store build identifier when available, classifier index hash when used, and
  fingerprints of the language resources used.
- If a game upgrade changes any source language resource or its inventory, the
  app identifies the generated files as stale and requires regeneration before
  install/modify. If a managed install is active, the app requires uninstall
  before regenerating against the updated baseline. If only version metadata
  changed while all source resources remain byte-identical, the app reports
  that regeneration is unnecessary.
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
For dialogue-only mode, validate the classifier against known in-dialogue
subtitles, overhead/oneliner text, item names, objectives, and ambiguous IDs
from the selected build; if those contexts cannot be established from its
structured resources, keep dialogue-only mode disabled.

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
