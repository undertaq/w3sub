# Witcher 3 Dual Subtitle Manager

A Windows desktop app for generating and managing a two-language `.w3strings` update for **The Witcher 3: Wild Hunt — Remastered 5.00**.

## Requirements and launch

- Windows with Python 3.10 or newer and Tkinter (included with the standard Windows Python installer).
- The game installed through Steam, GOG, or Epic Games, or its game folder available for manual selection.

From this project folder, run:

```powershell
python w3sub.py
```

This opens the GUI. Starting the program only discovers and scans game folders; it does not generate or install files automatically.

## Select and scan the game

At startup the app checks local Steam, GOG, and Epic install records, including Steam libraries/manifests, GOG registry records, and Epic manifests/registry records. It validates each candidate before showing it. If no candidate is found, or you want another copy, use **Browse** to select the game folder directly.

The scanner checks the executable under `bin\x64_dx12` or `bin\x64` and accepts Remastered **5.0.x** executable versions (the 5.00 release). For example, the local Steam installation reports `5.0.0.1044392`. The app also scans localized `.w3strings` resources under `content` and `dlc`. Whenever you select or browse to a folder, it rescans the executable version, store build when available, and language files. The language menus contain codes detected in that installation, so availability can differ with the installed content and DLC.

## Choose languages and generate a preview

Choose different **Primary** and **Secondary** languages from the detected menus. The primary language is the output resource and stays first in each merged record. In **Full text** mode, matching records are paired by string ID and key; the secondary text is appended with `<br>`. Primary-only records remain, while records that exist only in the secondary file are not added.

Full-text mode processes all matching records in the selected resources. Without a verified game-version-specific context index, it cannot distinguish dialogue from item names, menu/UI strings, overhead text, or other text. Those records may be combined too.

**Dialogue only** remains disabled until a validated structured dialogue-reference index exists for the detected game build. The app will not guess from wording, record IDs alone, or subtitle-like text.

The **built-in codec** is the default and requires only the Python standard library. It supports verified formats **162–163 (UTF-16LE)** and **164 (strict UTF-8)**. Before enabling generation, the app checks copies of every selected resource through decode/encode/decode, comparing container version, language key, every string ID and text, and every localization-key association. Generation repeats this gate over isolated copies. Embedded CR, LF, and CRLF text is preserved exactly through structured merging. Unknown formats or language keys, invalid Unicode, duplicate string IDs, and exact repeated key associations fail closed.

Native merging requires the same string ID and a shared actual localization-key hash. Strings without key associations remain primary-only. All primary associations survive, including distinct hashes for one ID and references to IDs stored in other resources. Dialogue-only changes additionally require every primary association for that string to be verified as a scene subtitle by a current index.

Use **Browse** beside the codec selection to explicitly choose an external executable for the legacy CSV path, or **Use built-in** to clear that saved override. The external path retains its line-oriented CSV limitations for embedded line breaks. The old `w3strings.exe` v0.4.1 supports only formats 162–163 and will fail compatibility checks on format 164. No converter download, .NET runtime, or third-party Python dependency is required. Codec attribution and the upstream MIT license are in `w3sub_app/third_party/w3strings_codec/`.

Once compatibility passes, choose **Generate preview**. Generated files and a record of the selected pair, game version, input hashes, codec kind, implementation path/version/SHA-256 (or external converter identity), and output hashes are written to application data, outside the game folder. Review the exact target paths and hashes shown by the GUI before continuing.

## Install, modify, and uninstall

- **Install** backs up each original target, saves its SHA-256 hash, then installs the reviewed generated files after an explicit confirmation.
- **Modify install** generates the new pair from original backups for any already-managed input resources. If a language changes from primary to secondary, its original file is used instead of the currently merged game file. Files no longer targeted by the new pair are restored from their backups.
- **Uninstall** restores originals only when each managed game file still matches the hash saved for the installed version. Original backups are retained after uninstall.

The app checks for a running Witcher 3 process before changing game files. It stores the game version and source-resource fingerprints at generation and install time, then compares them at startup and after a folder change. If source resources change after a game update, the preview is stale and must be regenerated. If an active install is stale, uninstall it safely before generating a replacement. A version metadata change with unchanged source resources is tracked separately from changed resource contents.

If a managed file or backup no longer matches its saved hash, the app reports the exact conflict paths and backup location and preserves the unexpected data. It will not overwrite a conflicting file during uninstall. Resolve the conflict manually, using the retained originals under the backup directory if recovery is needed, then rescan the game.

## App data and recovery files

On Windows, configuration, logs, generation records, staged outputs, manifests, and backups are stored under:

```text
%LOCALAPPDATA%\W3DualSubtitle\
```

The selected game path is saved in `config.json`. Per-game state is keyed by the normalized game-folder path:

```text
%LOCALAPPDATA%\W3DualSubtitle\games\<game-path-hash>\
```

Original install backups are kept under that per-game state folder in `backups\<install-id>\`. Keep this directory until you no longer need uninstall or manual recovery.

## Testing and live-game safety

The automated workflow test creates a disposable copy of a minimal game fixture under a temporary directory. It never runs install, modify, or uninstall against the live F: game folder. For normal GUI use, the app changes live game resources only after you explicitly confirm **Install** or **Modify**; **Uninstall** also requires its own confirmation.
