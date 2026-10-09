# Witcher 3 Dual Subtitle Manager

A Windows desktop app for generating and managing a two-language `.w3strings` update for **The Witcher 3: Wild Hunt — Remastered 5.00**.

The window title shows the program build version.

## Requirements and launch

- Windows. The standalone executable includes Python and Tkinter; no separate Python installation is required.
- The game installed through Steam, GOG, or Epic Games, or its game folder available for manual selection.

Launch `W3DualSubtitleManager.exe` from the executable download or build output. To launch from a local build:

```powershell
.\dist\W3DualSubtitleManager.exe
```

For source launch, install Python 3.10 or newer with Tkinter (included with the standard Windows Python installer), then run from this project folder:

```powershell
python w3sub.py
```

This opens the GUI. Starting the program only discovers and scans game folders; it does not generate or install files automatically.

## Build the Windows executable

Build on Windows x64 with Python 3.14 and Tkinter. PyInstaller builds for the platform running it; this recipe produces a single-file, windowed Windows executable.

```powershell
python -m pip install -r requirements-build.txt
.\build.ps1
```

The output is `dist/W3DualSubtitleManager.exe`. The build includes the app, Python/Tk runtime, built-in codec, and its license and notice. Game resources, per-user data, and optional external converters are excluded. Custom converters remain external executables selected at runtime with **Browse**. Configuration, logs, and generation data continue to use the per-user application-data folder described below.

## Select and scan the game

At startup the app checks local Steam, GOG, and Epic install records, including Steam libraries/manifests, GOG registry records, and Epic manifests/registry records. It validates each candidate before showing it. If no candidate is found, or you want another copy, use **Browse** to select the game folder directly.

The scanner checks the executable under `bin\x64_dx12` or `bin\x64` and accepts Remastered **5.0.x** executable versions (the 5.00 release). For example, the local Steam installation reports `5.0.0.1044392`. The app also scans localized `.w3strings` resources under `content` and `dlc`. Whenever you select or browse to a folder, it rescans the executable version, store build when available, and language files. The language menus contain codes detected in that installation, so availability can differ with the installed content and DLC.

When the language menus load, the app reads the active `TextLanguage` from the current user's `Documents\The Witcher 3\user.settings` (including a redirected Documents folder) and uses it as the default primary language when that resource is available. The secondary defaults to English when the primary is another language, or Chinese when the primary is English; if the preferred secondary is unavailable, the app chooses another installed language. Manual selections remain in place while the app is open; changing the primary only adjusts the secondary when both would otherwise match.

## Choose languages and generate a preview

Choose different **Primary** and **Secondary** languages from the detected menus. The primary language is the output resource and stays first in each merged record. Entries with no key hash in either language are treated as dialogue and paired by string ID; entries with keys are paired by string ID plus a shared key hash. The separator is based on the selected language text: the app checks the Western-language entry when the pair contains one, otherwise the primary entry. It inserts `<br>` only when that text ends with a locale-appropriate terminal punctuation mark (optionally followed by a closing quote); otherwise it inserts a space. Primary-only records remain, while records that exist only in the secondary file are not added.

Dialogue-only is the default mode. It merges entries with no key hash in both selected languages, matched by string ID. Full-text mode processes all matching dialogue and keyed records in the selected resources.

Hashless matching is a heuristic based on the decoded `.w3strings` association table. Any entry with no key hash in both selected files is treated as dialogue for this mode.

The keyless rule may classify non-dialogue text without key hashes as dialogue; the unmatched report helps inspect language identities that did not pair.

The **built-in codec** is the default and requires only the Python standard library. It supports verified formats **162–163 (UTF-16LE)** and **164 (strict UTF-8)**. Startup and selection changes check resource paths, pairing, and codec availability. During preview generation, the app checks isolated copies of every selected resource once through decode/encode/decode, comparing container version, language key, every string ID and text, and every localization-key association before publishing output. Embedded CR, LF, and CRLF text is preserved exactly through structured merging. Unknown formats or language keys, invalid Unicode, duplicate string IDs, and exact repeated key associations fail closed.

Full text matches keyless IDs by string ID and keyed entries by string ID plus a shared key hash. Dialogue only uses the same keyless-ID criterion and excludes keyed entries. Western terminal punctuation includes `.`, `..`, `...`, `…`, `?`, `!`, and `,`; Eastern locales recognize `..`, `...`, and `…` along with their corresponding punctuation marks. A new `unmatched_entries.csv` is saved in each generation folder and lists unmatched identities from both selected languages, including resource, language, side, ID, key hash, text, and reason. Use **Open unmatched list** in the preview to view it. All primary associations survive, including distinct hashes for one ID and references to IDs stored in other resources.

Use **Browse** beside the codec selection to explicitly choose an external executable for the legacy CSV path, or **Use built-in** to clear that saved override. The external path retains its line-oriented CSV limitations for embedded line breaks. The old `w3strings.exe` v0.4.1 supports only formats 162–163 and will fail compatibility checks on format 164. The built-in string codec needs no converter download, .NET runtime, or third-party Python dependency. Codec attribution and the upstream MIT license are in `w3sub_app/third_party/w3strings_codec/`.

Choose **Generate preview** to run compatibility validation and generate the preview. The preview status reports merged primary entries out of total primary entries processed and their percentage, plus the unmatched identity count. Generated files, the unmatched CSV, and a record of the selected pair, game version, input hashes, codec kind, implementation path/version/SHA-256 (or external converter identity), output hashes, and merge counts are written to application data, outside the game folder. Review the exact target paths and hashes shown by the GUI before continuing.

### Cutscene subtitles (`.subs` and `.usm`)

**Include cutscene subtitles** is on by default. Clear it before generating a preview to produce only interactive `.w3strings` files. When enabled, the app scans active game bundles for `.subs` sidecars and `.usm` movies. Changed sidecars and their full companion `.usm` files are packed together into one `movies.bundle` with a matching `metadata.store`; the game requires the full movie alongside the sidecar override. Sidecars are paired by movie name, playback folder (`subs` or `altsubs`), and locale suffix. The two playback folders are processed independently because their cue text and counts can differ; an alias is emitted only when the corresponding primary-language resource is absent.

Cues are paired only when their start and end times match exactly. Duplicate timing pairs are matched one-to-one in source order. A cue without a partner stays in the primary language; unmatched cues from either language and resources that could not be processed are listed in that generation's `cutscene_unmatched.csv`. The preview's **Cutscene unmatched CSV** button opens the report. It includes resource, locale, cue times, text, and a reason for unmatched cues or skipped resources.

For embedded USM tracks, locale IDs have been confirmed for `en`, `pl`, `de`, `it`, `fr`, `cz`, `es`, `zh`, `ru`, `cn`, `jp`, `kr`, `br`, `esmx`, and `ar`. Selected locale codes without a confirmed USM locale ID, including `hu`, `tr`, and `ua`, are skipped and reported; unselected numeric locale tracks are preserved. Unsupported USM chunk/table layouts, multiple subtitle streams, unverified timing profiles, or merged text exceeding the movie's existing subtitle capacity are also reported as skips. Sidecars are skipped if a selected locale is missing, the copies disagree, the file exceeds the 16 MiB parsing limit, or its encoding/rows do not match the recognized CRI format. Bundle integrity or extraction failures stop generation instead of publishing a partial preview.

The second language follows the primary text after a carriage return (`U+000D`) inside `.subs` sidecar cues. Cue records remain CRLF-delimited. Embedded USM subtitle cues use the same carriage-return line break, confirmed in the recap movie. The app does not insert HTML `<br>` markup into cutscene files. Embedded USM patching preserves the original cue's locale and timing, and updates only validated subtitle bytes, container sizes, padding, and video seek offsets.

Cutscene generation hashes bundle indexes and the contents of bundles that contain candidate cutscene resources. Startup freshness checks use bundle metadata for a fast scan; generation reports byte progress while hashing and processing resources, and Install rechecks the source content before changing files. This can read several GB and take a while. The preview reports estimated scan/work bytes, the actual `movies.bundle` package size, and cue match counts. Movie resources and subtitle sidecars remain uncompressed in the generated bundle to match the shipped movie-bundle layout. Full movie files are large: the 5.00 installation used for development contains about 7.2 GiB of unique USM video payloads, while the selected-language package can be smaller. Generated outputs, temporary bundle creation, install staging, and the installed Mods copy need additional space; the exact package size appears in the preview.

The opening recap was verified in game with its modified `.subs` and full `.usm` packaged together: both languages displayed on separate lines. Other cutscenes and language pairs still need in-game checks. For the observed zh+cn set, 27 of 30 subtitle-bearing USMs met the current static layout, timing, and capacity checks; two exceed verified subtitle capacities, and one has an unverified nonzero timing-header value. Other language pairs can have different capacity skips. The live preview report shows the result for the selected game and languages.

## Install, modify, and uninstall

- **Install** writes interactive `.w3strings` plus `content/bundles/movies.bundle` and `content/metadata.store` under `Mods/modW3DualSubtitleManager`. The game's base `content` and `DLC` files remain untouched. The manager checks that each target is new or already belongs to its active install before writing it.
- **Modify install** updates the manager-mod overrides to match the new preview. Generation reads the unchanged base-game string resources.
- **Uninstall** removes manager-mod files only when they still match the installed hashes. The base game files are left untouched.

The app checks for a running Witcher 3 process before changing game files. It stores the game version and source-resource fingerprints at generation and install time, then compares them at startup and after a folder change. If source resources change after a game update, the preview is stale and must be regenerated. If an active install is stale, uninstall it safely before generating a replacement. A version metadata change with unchanged source resources is tracked separately from changed resource contents.

Historical generation records and manager-mod install manifests remain readable. A legacy classifier digest does not affect freshness or safe uninstall. Direct game-file installs are not managed by this version; restore those files through the game's storefront before using the manager.

If a managed file no longer matches its saved hash, or the dedicated Mods folder contains files the manager does not own, the app reports the exact conflict paths and preserves the unexpected data. It will not overwrite or delete conflicting files during Modify or Uninstall. Resolve the conflict manually, then rescan the game.

## App data and recovery files

On Windows, configuration, logs, generation records, staged outputs, and manifests are stored under:

```text
%LOCALAPPDATA%\W3DualSubtitle\
```

The selected game path is saved in `config.json`. Per-game state is keyed by the normalized game-folder path:

```text
%LOCALAPPDATA%\W3DualSubtitle\games\<game-path-hash>\
```

## Testing and live-game safety

The automated workflow test creates a disposable copy of a minimal game fixture under a temporary directory. It never runs install, modify, or uninstall against a live game folder. For normal GUI use, the app changes manager-mod files only after you explicitly confirm **Install** or **Modify**; **Uninstall** also requires its own confirmation.
