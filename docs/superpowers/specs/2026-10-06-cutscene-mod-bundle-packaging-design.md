# Cutscene Mod Bundle Packaging Design

**Status:** Draft for user review
**Date:** 2026-10-06
**Scope:** Make generated `.subs` and `.usm` dual subtitles load as Witcher 3 Remastered 5.00 movie overrides

## Intent and success criteria

The goal is for a movie that currently displays only the primary-language subtitle to display the generated primary and secondary text together. The existing merge logic stays responsible for cue pairing and text changes. The new work packages those changed movie resources in a form the game loads from the manager mod.

Success means:

- The generated recap sidecar displays both `zh` and `en` in the initial movie when the game language is `zh`.
- At least one embedded-USM subtitle is also verified in game, with its timing and audio/video intact.
- Generated movie resources are installed and removed through the same conflict-aware manager lifecycle as the interactive strings.
- The original game bundles remain unchanged. No persistent original-file backups are introduced.
- The packager is compatible with the selected 5.00 game version and does not require a REDkit installation at runtime unless the user explicitly chooses an external-tool mode.

## Findings behind this design

The recap movie's embedded USM subtitle track contains Chinese followed by English. The user confirmed that U+000D displays both lines in embedded USM cues. Storybook movies such as `st_1.usm` have no embedded `@SBT` subtitle track and depend on `.subs` sidecars. Sidecars are line-oriented; keep cue text on one physical row and separate languages with ` / ` so the game does not discard the subtitle file.

The current installer maps generated cutscene paths to loose files under `Mods/modW3DualSubtitleManager/content/<resource path>` ([install.py](/D:/project/w3sub/w3sub_app/install.py:656)). The linked [Witcher3-MovieSubs-Tool README](https://github.com/Outsidev/Witcher3-MovieSubs-Tool/blob/master/README.md) describes building movie mod bundles and requiring a Witcher Tools generated `metadata.store`. CDPR's [REDkit publishing guide](https://cdprojektred.atlassian.net/wiki/spaces/W3REDkit/pages/6328254/Publishing+mods) also describes creating mod bundles and metadata while cooking a mod.

The linked tool is not a safe drop-in dependency. Its source dates to 2016, hardcodes English as the target track, assumes fixed bundle paths, and deletes `content\\patch0` and `DLC\\substr` directories during startup ([Program.cs](https://github.com/Outsidev/Witcher3-MovieSubs-Tool/blob/master/Witch3rSubman/Program.cs), [BundleFiles.cs](https://github.com/Outsidev/Witcher3-MovieSubs-Tool/blob/master/Witch3rSubman/BundleFiles.cs)). The project has no release artifact in its repository page. Its packaging approach is useful evidence; its implementation should not be executed against or copied into the live manager.

At design time, the selected game root on this machine has no active manager install or manager `Mods` directory. Staged generations do exist. In-game verification therefore needs a new packaged preview and install after the packaging path is implemented.

## Approaches considered

### A. Use REDkit/WCC to cook the mod

Generate a REDkit project or staging tree and invoke the supported cooker to create the mod bundle and metadata.

- **Advantages:** Game-aware packaging and metadata generation; aligned with CDPR's documented publish workflow.
- **Costs:** Requires discovering and supporting REDkit/WCC installation and version detection; likely adds a substantial external dependency and makes the standalone build incomplete without it.
- **Assessment:** Keep as a validation/reference backend or optional fallback, not the default user path.

### B. Add a focused manager-owned packager

Reuse the existing POTATO70 inventory/extraction and merged `.subs`/`.usm` outputs, then create a minimal mod bundle containing only changed resources and its metadata. Validate it with an independent reader and the game before enabling it by default.

- **Advantages:** Preserves the standalone workflow, avoids rewriting base bundles, and can emit compact bundles containing changed movie resources only.
- **Costs:** Bundle metadata is version-sensitive and must be proven against 5.00. The packaging writer is a new subsystem and needs strict validation.
- **Assessment:** Recommended, gated on a small copied-resource prototype proving bundle and metadata acceptance by the game.

### C. Vendor or adapt the linked C# utility

Modify its fixed English-to-Turkish replacement flow to accept dual language text and fit the manager lifecycle.

- **Advantages:** Demonstrates bundle rebuilding and metadata updates for old game data.
- **Costs:** Hardcoded paths/language, destructive cleanup, old framework, no clear license, assumptions about older bundle layouts, and no general dual-language merger.
- **Assessment:** Reject as an implementation dependency. Use its output structure and lessons only.

## Recommended architecture

### Generation and provenance

Keep the current `.subs` and `.usm` merge algorithms. Extend the generation inventory so every changed virtual resource retains its originating source bundle identity as well as its generated path and hash. This provenance is needed to build the appropriate override bundle and to group same-named source bundles safely.

The existing USM patcher continues to modify only the selected primary locale track, appending secondary text at exact matching cue times using the verified carriage return line break. The sidecar merger continues to pair exact start/end times, keeping both languages on one cue row with a visible inline separator. Packaging must not change cue timing, text, locale IDs, or the already merged payload bytes.

### Package layout

Package changed movie resources by source-bundle group. Each group produces a collision-safe, `mod`-prefixed manager sub-mod with its own bundle and `metadata.store`, because different source bundles can share names such as `movies.bundle` or `blob.bundle`.

The exact folder and bundle naming convention is a prototype decision. The first candidate is a stable generated folder name derived from a normalized source-bundle identity, for example `Mods/modW3DualSubtitleManager_content0_movies/`. The output bundle should contain only generated overrides from that source bundle, not a copy of the full original bundle. The resource metadata must preserve the original depot paths expected by the game.

The existing interactive string overrides can remain in `Mods/modW3DualSubtitleManager`. Each folder begins with `mod`, as required by the [REDkit mod guide](https://cdprojektred.atlassian.net/wiki/spaces/W3REDkit/pages/36339714/Managing+mods). Multiple cutscene sub-mods are treated as one logical install by the manager.

### Metadata and packager boundary

The packager must produce the bundle index and `metadata.store` entries as a matched set. Never publish one without the other. Support one backend behind a small interface so generation and install lifecycle code do not depend on a particular external executable.

Before selecting the final backend, a prototype must show that a tiny copied-resource bundle can be read back with the exact resource paths and hashes and loaded by the 5.00 game. Candidate backend order:

1. Test whether the already bundled WolvenKit CLI can produce the required mod bundle and metadata without an installed REDkit. The bundled helper documentation currently describes a CR2W reference-scanning protocol, not a packaging contract, so capability must be verified rather than assumed.
2. If unavailable, prototype a narrow independent 5.00 POTATO70/metadata writer using the current bundle reader and fixture data.
3. Use REDkit/WCC as an optional external backend if the standalone writer cannot generate metadata that the game accepts. The GUI must then state the missing-tool requirement before generation.

Do not call the linked C# program, copy its source into this repository, or allow any packager to delete, rewrite, or rename files under the base game `content` or `dlc` trees.

### Install manifest and lifecycle

Treat each generated mod bundle and its `metadata.store` as managed targets with their own installed hashes and bundle-group identity. Extend the manifest schema to distinguish packaged cutscene targets from loose interactive-string targets and to record the packager identity/version used to build them.

- **Install:** Stage and validate all generated package files first, check for existing unmanaged targets, then transactionally publish every bundle and matching metadata file.
- **Modify:** Replace manager-owned packages, add new bundle groups, and remove obsolete groups only when their current hashes still match the manifest.
- **Uninstall:** Remove only manager-owned bundle and metadata targets whose hashes still match. Leave base bundles and user-created mod files alone.
- **Failure recovery:** Keep only the existing transaction-local rollback snapshots. A partially published bundle group must not be treated as active.

If `mods.settings` explicitly disables a generated sub-mod, show that state in the GUI. Do not create or rewrite the user's settings file automatically. CDPR's guide says `mods.settings` controls overrides while mods are enabled by default when no disabling entry exists.

### Preview and user flow

Preview details should show the source bundle group, resulting mod bundle path, package size, number of changed `.subs`/`.usm` entries, and any resources excluded by the packager. Keep the existing unmatched-cue CSV and per-resource skip reasons.

Before Install or Modify, the confirmation lists the concrete bundle and metadata targets. Conflicts are reported by exact target path and stop the operation. A game-version or source-bundle fingerprint change makes the package stale and requires regeneration.

## Prototype and acceptance plan

The prototype runs only on copied/staged resources, never on the live game's base bundle paths. It should:

1. Select the source bundle containing `movies/cutscenes/gamestart/subs/recap_wip_zh.subs` and one USM containing matched `zh`/`en` cues.
2. Build a small mod package with the two generated resources and required metadata.
3. Re-read the package, confirm both depot paths, compare the unpacked payloads byte-for-byte with the already merged generation outputs, and verify bundle bounds and hashes.
4. Install the package under a disposable mod folder in a copied game tree or another isolated game profile.
5. Verify the opening recap displays both lines and one embedded USM displays both lines without changing its cue timing or audiovisual streams.
6. Exercise Modify and Uninstall on the copied install and confirm the original game bundles are byte-identical before and after.

Do not claim movie support works until step 5 succeeds in game. If the package reads back correctly but the game still loads only the original language, investigate mod activation/load order and the game's bundle override resolution before changing cue merge rules.

## Risks and open questions

- The exact REDengine 4 `metadata.store` requirements for 5.00 need validation; old tool behavior alone is not enough evidence.
- Bundle groups may have duplicate virtual resource names or different source bundles with equal basenames. Group identity and mod output names must avoid collisions.
- Bundle packing may need to preserve source chunk compression flags, hashes, path tables, and alignment rules that are not represented in the cutscene generation record today.
- Generated USM files can be large. Packaging must stream data and report progress by bytes; it must avoid materializing full videos in memory.
- The current in-game install state is inactive on this machine, so a successful acceptance run requires installing a newly generated package in a controlled game profile.
- If only REDkit/WCC can create accepted metadata, a truly self-contained standalone manager may not be feasible without redistributing a separately licensed helper.

## References

- [Witcher3-MovieSubs-Tool README](https://github.com/Outsidev/Witcher3-MovieSubs-Tool/blob/master/README.md)
- [Witcher3-MovieSubs-Tool Program.cs](https://github.com/Outsidev/Witcher3-MovieSubs-Tool/blob/master/Witch3rSubman/Program.cs)
- [Witcher3-MovieSubs-Tool BundleFiles.cs](https://github.com/Outsidev/Witcher3-MovieSubs-Tool/blob/master/Witch3rSubman/BundleFiles.cs)
- [CDPR REDkit: Managing mods](https://cdprojektred.atlassian.net/wiki/spaces/W3REDkit/pages/36339714/Managing+mods)
- [CDPR REDkit: Publishing mods](https://cdprojektred.atlassian.net/wiki/spaces/W3REDkit/pages/6328254/Publishing+mods)
