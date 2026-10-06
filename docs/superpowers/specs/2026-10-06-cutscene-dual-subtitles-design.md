# Cutscene Dual Subtitles Design

**Status:** Design approved; awaiting spec review before implementation planning
**Date:** 2026-10-06
**Scope:** The Witcher 3 Remastered 5.00 cutscene subtitles in `.subs` sidecars and `.usm` embedded subtitle chunks

## Problem

The manager currently generates and installs dual-language `.w3strings` files for interactive game text. Cutscene subtitles use different resources: timed `.subs` sidecars and CRI USM videos containing `@SBT` subtitle chunks. Neither format currently participates in the manager's generation or install lifecycle.

The current install layer only accepts existing `.w3strings` targets. Cutscene resources are primarily in POTATO70 v5 bundles, and writing back into the original game bundles is out of scope. The 5.00 installation inspected for this design has 1,122 `.subs` entries and 138 distinct bundled `.usm` paths. Its `.usm` payloads total approximately 9.2 GB uncompressed, so generation cost and output size must be visible to the user.

## Goals

- Merge the selected secondary language into the selected primary language for both `.subs` and embedded USM subtitles.
- Keep cue timing intact and avoid attaching translations to the wrong line.
- Preserve all video and audio payload bytes. Do not re-encode video or audio.
- Stage generated cutscene files in the existing generation preview and install them through a reversible, conflict-aware lifecycle.
- Record the game version and source bundle identity used for generation, and detect stale cutscene outputs after a game update.
- Report cue totals, matched cues, match ratio, unmatched cues, and generated media size.
- Keep cutscene work optional in the GUI, enabled by default for new generations, with an estimate and a way to disable it before generation.

## Non-goals

- Rebuild or replace original game bundles.
- Add new game subtitle languages or change the language selected by the game.
- Re-encode USM video or audio streams.
- Translate content automatically.
- Reuse or copy implementation code from other localizer projects. Format behavior may be researched and independently implemented.

## Proposed design

### 1. Discover source resources from bundles

Use the existing POTATO70 v5 bundle inventory and extraction code to locate `.subs` and `.usm` resources in `content` and `dlc` bundles. Add a streaming extraction path for large entries rather than materializing every USM payload in memory. Inventory work should be cached using the game version and source bundle identity so repeated generations do not rescan unchanged resources unnecessarily.

Do not require WolvenKit, WCC Lite, or a CRI encoder for this workflow. The existing bundle reader is sufficient to find and read the source entries. A malformed, unsupported, or unverified bundle entry must produce a clear generation error rather than a partial output.

### 2. Merge `.subs` sidecars

Parse the shipped sidecar form as UTF-16LE with BOM and CRLF records. Preserve the time-unit header, comments, record ordering, encoding, and line endings. Parse each cue by its first two commas so commas in the subtitle text are retained.

Pair language files by virtual resource stem and cue by the exact `(start, end)` time pair. Merge only exact timing matches. Keep a primary cue unchanged when no secondary cue matches it, and do not append secondary-only cues with no primary timing anchor. Record all unmatched primary and secondary cues with resource path, language, cue times, text, and reason in a cutscene debug CSV.

Where the same sidecar resource is used through `subs` and `altsubs` playback paths, emit identical managed overrides for both paths. Do not duplicate an override when the source inventory already contains the equivalent logical path.

### 3. Patch embedded USM subtitles

Walk USM chunk boundaries using the CRI chunk size fields. Parse `@SBT` records and their subtitle cue metadata, including the locale ID, start time, duration, text byte count, encoding, and padding. Convert cue timing to `(start, start + duration)` and merge only an exact time-range match from the selected secondary locale.

Patch the existing primary-locale `@SBT` cue text in place logically: rebuild only changed subtitle chunks, update their text lengths, chunk lengths, and alignment padding, and preserve all non-subtitle chunks byte-for-byte. Update any container length or offset metadata affected by changed chunk sizes. If a USM layout is unknown, malformed, or cannot be safely rebuilt while preserving video/audio data, skip that file with a clear reason and list it in the report; never write a guessed patch.

Use the game's locale mapping for USM language IDs. The inspected `fb_1a.usm` sample maps Traditional Chinese `zh` to locale ID 7 and Simplified Chinese `cn` to ID 9. Treat the mapping as explicit data, not as an assumption based on language ordering. If a selected locale is absent from a video, leave that video unchanged and report it.

For dual text, place the primary text first and the secondary text after a native line break. Do not write HTML such as `<br>` into either format. USM subtitle payloads must use CRI's line-break representation; sidecar serialization must preserve cue-row boundaries while representing a line break in cue text.

### 4. Stage and preview output

Extend generation metadata to include the cutscene option, selected locales, resource inventory, relevant bundle fingerprints, output hashes, and game version. Keep generated cutscene overrides under a separate virtual-resource tree within the generation directory, retaining their original depot paths.

The preview should report:

- sidecar and USM resources discovered, changed, skipped, and unchanged;
- primary and secondary cue counts, exact matches, merge ratio, and unmatched counts;
- output size for changed sidecars and full changed USM files;
- estimated maximum work/output size before generation and actual bytes written during generation;
- a CSV path for unmatched and unsupported cutscene entries.

Because inspecting every bundled USM can process several gigabytes, progress must advance by bytes or entries actually read and written, not by elapsed time. Cache safe inventory results against the executable version and relevant bundle metadata; revalidate source identity when producing and installing a generation.

### 5. Install as a managed mod override

Install cutscene outputs under a dedicated folder such as:

```text
<game>\Mods\modW3DualSubtitleManager\content\<original depot path>
```

Keep base bundles and loose game files untouched. The install manifest must distinguish generated mod files from the existing `.w3strings` targets, store hashes for installed files, and track the generation/game version. An unmanaged pre-existing mod folder or conflicting file must be reported rather than overwritten.

Modify should stage and swap only manager-owned files. Uninstall should remove only files whose current hashes still match the manager's installed hashes; if a user or another mod changed a managed file, preserve it and report a conflict. Remove only empty directories created for this install. No backup copy of original game media is needed because originals are never replaced; generated files and rollback snapshots remain in app state.

CD Projekt Red's REDkit documentation states that mod directories under the game's `Mods` folder are loaded when their names begin with `mod`. The generated resource tree follows that convention and uses the game's virtual resource paths.

### 6. Version freshness

Persist the executable game version and source bundle identity used for generation and installation. On startup and after selecting a game folder, compare the current version and bundle inventory with the saved generation. Mark cutscene output stale when the game version or relevant bundle identity changes, and offer regeneration before Modify or Install.

The startup check should remain cheap: use the executable version and bundle metadata for the fast check, then perform content hashing/revalidation during generation and before install. It must not decompress the USM collection at every startup.

## Matching and output policy

- Sidecar cue key: exact `(start, end)`.
- Embedded USM locale ID selects the language track; cue matching is exact `(start, start + duration)` across the selected tracks.
- Merge direction: selected primary locale receives primary text followed by secondary text.
- Unmatched primary cue: preserve unchanged and report.
- Unmatched secondary cue: do not insert without a primary timing anchor; report.
- Unsupported resource: preserve source, skip generated override, and report the reason.
- Separator: native line break for the specific subtitle format; never HTML markup.

## Risks and open implementation details

- USM files can have chunk sizes, alignment padding, and internal indexes. The writer must establish the exact affected fields from shipped 5.00 resources and refuse formats it cannot safely rewrite.
- `.subs` and embedded USM subtitles may both exist for one cutscene. They appear to be alternate playback paths, but an in-game check is needed to ensure the game does not display both overrides simultaneously.
- USM locale IDs do not include every `.w3strings` language in every movie. Mapping must be explicit, and absent/unsupported locales must be surfaced in preview.
- Full changed USMs are copied into the mod override, so the additional disk use can be substantial even though only subtitle chunks change.
- Mod load order or an existing mod overriding the same virtual resource can take precedence. The GUI should disclose conflicting paths and show where the generated mod is installed.
- The exact sidecar cue-text line-break representation must be confirmed against the game's shipped `.subs` reader behavior before broad output is generated.

## References

- CRI Sofdec subtitle information file format: <https://game.criware.jp/manual/native/sofdec2/latest/usr_cmew_data3.html>
- CRI Sofdec subtitle channel options: <https://game.criware.jp/manual/native/sofdec2/latest/usr_console_encoder_main.html>
- FFmpeg CRI USM reader, including `@SBT` subtitle chunks: <https://ffmpeg.org/doxygen/trunk/usmdec_8c_source.html>
- CD Projekt Red REDkit guide to installing mods: <https://cdprojektred.atlassian.net/wiki/spaces/W3REDkit/pages/36339714/Managing+mods>
- Installed 5.00 sample resources and bundle inventory inspected locally in `F:\Game\SteamLibrary\steamapps\common\The Witcher 3`.
