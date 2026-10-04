# Dialogue Index and Keyless Merge Integration

Date: 2026-10-04

Status: Draft for user review

Supersedes the dialogue-classifier implementation details in
docs/superpowers/plans/2026-10-03-dual-subtitle-manager.md where this addendum
adds keyless native merging and a bundled CR2W parser helper. The original
product goal and install safety rules remain in force.

The user's approval to bundle WolvenKit applies only to this separate CR2W
inspection helper, under its GPLv3 terms. It does not change the native
w3strings codec or add a bundled external string converter.

## Goal

Enable Dialogue-only generation for supported Remastered 5.00 installations
only when a complete, current index proves that an ID is used exclusively as
spoken scene dialogue. Keep menu, item, overhead, objective, choice, mixed-use,
unknown, and unparsed-resource IDs unchanged in the primary language.

The existing native merger matches records through shared localization-key
hashes. That leaves explicit spoken lines with no key associations unmerged.
This addendum adds a narrowly guarded ID-only path for those records.

## Evidence and limits

The local prototype found 79,790 IDs shared by the installed en/zh files with
no localization-key association. Of these, 64,832 appeared in typed
CStorySceneLine.dialogLine fields, 7,145 appeared in
CStorySceneChoiceLine.choiceLine fields, and 7,813 appeared in neither field
among parsed scenes. Therefore keylessness is only an eligibility condition;
it never classifies dialogue by itself.

The prototype inspected 31 content0 bundles and 6,380 physical w2scene entries.
An independent parser read 6,379 scenes. A WolvenKit 7.2.0 CLI build with its
CR2W ceiling raised to 164 successfully exported a sample scene and its typed
dialogue fields. The current evidence does not prove full content/DLC coverage
or rule out an ID being reused by a non-dialogue resource. See
docs/dialogue-classifier-prototype.md.

## Parser helper and distribution

- Ship WolvenKit as a separate Windows helper process, not as a library linked
  into the Python application. The helper is used only to inspect game
  resources; the existing native w3strings codec continues to read and write
  localization files.
- Pin the validated source base to WolvenKit/WolvenKit-7 commit
  8eb4026349b1e419c189d42b8286b4a5613ab433. The prototype patch changes the
  CR2W upper-version check from 163 to 164 in both read paths and updates the
  corresponding diagnostic. A later upstream build may replace this patch
  only after it passes the same v164 corpus checks.
- Include the helper's license and third-party notices. Any binary release
  must provide the complete corresponding source for the exact pinned helper
  build, including the v164 and batch-processing patches, through the same
  release channel. Do not ship the binary if that source delivery cannot be
  provided.
- Add batch inventory support to the helper so indexing does not launch a new
  CLI process for every game resource. Keep conversion output in temporary
  application state and retain only IDs, contexts, source identities, hashes,
  and diagnostics in the persistent index; do not store localized game text.
- Validate helper identity and v164 support before indexing. A missing,
  modified, incompatible, or failed helper makes the index unavailable.

## Bundle scan and context index

- Inventory active bundle files under content and, when present, dlc. Never
  treat dlc-tombstones as active content. Include duplicate depot paths as
  separate physical entries so every use contributes to context.
- Parse the complete bundle-entry inventory and every supported structured
  resource that can contain a localized-string reference. Verify resource
  bounds, decompressed size, and CRC before handing data to the helper.
- Read typed LocalizedString references from the WolvenKit structured output.
  Classify CStorySceneLine.dialogLine as SCENE_SUBTITLE and
  CStorySceneChoiceLine.choiceLine as CHOICE_UI. Treat every other typed
  localized reference as a non-dialogue context. Union usage across physical
  resources; a string ID with more than one context is AMBIGUOUS.
- The reference scan is complete only if every in-scope structured resource
  that can carry localization references is inventoried and parsed. Any
  unsupported resource type, parse error, malformed typed reference, missing
  bundle, or incomplete inventory fails the entire build closed. Do not
  publish a partial index or enable Dialogue-only mode.
- Extend the index API with an ID-level context lookup (for example,
  context_for_id). Keep key-identity lookup available for compatibility, but
  derive its classification from the complete aggregate usage of that ID.
  Bump the index schema and digest so caches built with the old keyed-only
  representation cannot be mistaken for complete indexes.
- Store the index in the app's per-game state directory. Its schema and digest
  include the detected game version, sorted physical resource inventory,
  hashes of all structured resources that contribute references, context
  assignments, and helper/parser identity. A cached index is current only
  when the game remains in supported major/minor 5.0 and its complete source
  inventory and content hashes match.
- Build or refresh the index on a worker when the user requests Dialogue-only
  mode or selects an explicit Rebuild action. Show progress and keep the GUI
  responsive. On startup and after game-folder selection, check the cached
  index; if missing, stale, or invalid, keep Dialogue-only disabled and show
  the reason and rebuild action. A changed game version or resource inventory
  requires a fresh index before dialogue generation.

## Merge behavior

- Preserve full-text behavior and require the existing shared-key pairing for
  every keyed record. In dialogue-only mode, also require the current index to
  identify the ID as exclusively SCENE_SUBTITLE; all unknown, mixed, or
  non-dialogue IDs remain primary-only.
- Add ID-only native merging only when the same ID exists in both selected
  language files, neither file has any localization-key association for that
  ID, and the current complete index classifies that ID as exclusively
  SCENE_SUBTITLE.
- If one language has keys and the other does not, or the ID is absent from
  either language, do not use ID-only fallback.
- Keep CHOICE_UI excluded. Keyless IDs that are not explicit spoken-line
  references, and IDs with any other or ambiguous use, remain primary-only.
- Keep the external CSV converter path fail-closed for keyless matching. CSV
  rows do not provide authoritative per-file key-association inventories for
  this new ID-only decision.
- Continue recording the index digest and schema version in every dialogue
  generation record. Recheck index freshness before publishing staged output
  and before install, using the existing generation/install safeguards.

## User-visible behavior

- A missing or stale index never silently falls back to a heuristic or
  full-text merge.
- The UI reports index state as building, ready, stale, or unavailable and
  explains parse failures with the affected bundle/resource path.
- Full-text generation remains available when the index helper is missing or
  any indexed source cannot be parsed.

## Acceptance criteria

- A complete scan of the supported 5.00 bundle roots produces a deterministic
  index and refuses to mark it complete when any reference-bearing resource
  fails inventory or parsing.
- The index distinguishes direct spoken lines, choice UI, other localized
  references, mixed use, and unknown IDs without inspecting translated
  wording.
- Native dialogue merging combines only shared keyed scene IDs under the
  existing shared-key rule, or truly keyless IDs under the new dual-language
  ID-only guard.
- Keyed menus/items, keyless choice text, non-line keyless text, mixed IDs,
  language mismatches, and stale/unknown IDs remain byte-for-byte primary
  text.
- Cached indexes become stale when the supported version boundary, physical
  bundle inventory, contributing resource bytes, or parser identity changes.
- The UI never enables Dialogue-only mode for an incomplete or stale index.
- The helper build is pinned and reproducible, separate from the Python
  process, and its binary, patches, notices, and corresponding source are
  distributed together.
- Generation and install remain confined to existing staging and managed
  backup flows; the live game is read-only during index construction.

## Verification scope

Validate the helper against every in-scope reference-bearing asset from the
known Remastered 5.00 installation, including installed DLC when present.
Cross-check typed references from representative dialogue, choice, menu,
item, overhead-text, and objective resources. Exercise mixed-use IDs and
failed/incomplete scans through copied fixtures. Verify the generated native
files and install lifecycle only against isolated game copies; do not install
onto the live game as part of implementation.
