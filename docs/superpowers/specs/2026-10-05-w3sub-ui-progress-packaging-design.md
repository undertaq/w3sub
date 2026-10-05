# W3 Dual Subtitle Manager UI, Progress, and Packaging Design

Date: 2026-10-05

Status: Draft for user review

## Goal

Make the Windows desktop app quicker to start, easier to operate, clearer during
long operations, and distributable as a single standalone executable. Dialogue-only
merge becomes the default. The optional dialogue-index build action is removed;
new dialogue-only generations continue to use the existing keyless-entry rule.

The application must retain its managed install, modify, and uninstall safeguards,
including exact target verification and original-file backups.

## Current behavior and constraints

- The app is a Tkinter GUI launched through `w3sub.py` and has no application
  packaging recipe.
- Startup and several selection changes automatically run a full codec round trip
  over every selected language resource. The GUI also performs a compatibility
  round trip immediately before preview generation. Native generation runs its
  own compatibility round trip again, duplicating work.
- The GUI disables its controls during background work and shows an indeterminate
  progress animation. Only dialogue-index building currently reports completed
  counts.
- Snapshot loading reads dialogue-index state even though the current keyless
  merge policy does not use that index to select entries.
- Dialogue install freshness still checks for an index digest. This conflicts
  with keyless-based new generation records, whose classifier digest is empty.
- The preview is a tree in the main window, with eight visible rows and a minimum
  window size of 760 by 630 pixels.
- The built-in codec is implemented in the app. An external converter remains an
  optional user-selected override.

## Approaches considered

1. **Defer the full compatibility check to generation (selected).** Perform only
   quick path, codec-availability, and resource-pair checks during startup and
   folder selection. Run the complete semantic round trip once in the generation
   worker, with progress. This keeps launch responsive and preserves the full
   compatibility gate before generated files are published.
2. **Keep checking every resource at startup.** This retains current semantics but
   does not address the slow startup users experience.
3. **Add a manual compatibility-check action.** This makes the check explicit,
   but adds a separate step and still requires clear progress and error handling.

## Dialogue-index removal and merge behavior

- Remove the **Build optional Dialogue Index** button, index status display,
  progress messages, and worker flow from the GUI.
- Do not load or validate an index during startup or game-folder selection.
- Keep new Dialogue-only merges based on the existing rule: eligible entries have
  no key hash in either language and match by string ID. Keyed non-dialogue
  entries remain outside Dialogue-only mode.
- Remove the index digest as a freshness requirement for dialogue generations
  and installs. Preserve parsing of existing generation records and install
  manifests, but treat any saved classifier digest as historical provenance,
  not as a requirement for currentness or safe restoration. Source fingerprints,
  managed target hashes, validated original backups, and transaction checks
  remain the lifecycle safety checks.
- Allow uninstall of legacy active installs without loading or rebuilding an
  index. Safety remains based on managed target hashes, validated original
  backups, transaction checks, and the existing conflict handling.
- The WolvenKit helper and its source, licenses, and notices remain repository
  artifacts but are not invoked by the new app workflow or bundled into the
  standalone executable. Pruning those historical assets is outside this change.

## Compatibility-check flow

- At startup and after a game folder or language pair changes, check that the
  selected resources exist and pair by relative resource directory, and that the
  selected codec is available. Do not decode, encode, or round-trip the complete
  inventory in these UI paths.
- Do not gate the Generate button on a completed full compatibility scan. The
  preview operation performs the safety check before it publishes any output.
- Move the compatibility validation into the generation pipeline so it runs once
  for both the built-in codec and an external converter. Use isolated input copies
  and preserve the semantic comparison of format, language key, string IDs, text,
  and localization-key associations.
- If compatibility fails, abort generation and preserve the existing diagnostic
  behavior. Report the failing stage and resource in the operation status/log.
- Remove the GUI preflight that currently repeats the generation check. Do not
  add a persistent compatibility cache in this change.

## Progress reporting

- Add a structured worker-to-GUI progress event carrying the operation label,
  current phase, completed work units, and total work units. Keep Tk updates on
  the existing UI-thread queue poller.
- Use determinate progress whenever a phase has a known total. Display both the
  phase and counts, for example “Checking resources 8/36,” “Merging resources
  5/18,” or “Staging files 4/12.” The fraction must come from completed work,
  never elapsed-time estimates.
- Emit progress for generation phases (compatibility, resource decode/merge,
  output encode/write) and file lifecycle phases (backup, snapshot, staging,
  replacement, restore). Include startup candidate scanning when its candidate
  count is known.
- Progress is phase-scoped. At a phase boundary, show the new phase and its own
  count rather than implying a weighted total-operation percentage. If a phase
  cannot report a meaningful total, show its activity text without fabricating a
  percentage.
- Clear progress state on success and error. Do not emit the removed dialogue
  index progress events.

## GUI layout and guidance

- Set an initial window size around 900 by 560 pixels, with a minimum around 760
  by 520 pixels. Keep the window resizable and retain grid expansion for the
  preview area.
- Set Dialogue-only merge before Full-text merge and select it by default.
- Remove index-related rows so they do not consume space. Reduce the preview
  tree to about five visible rows and retain scrolling for longer output lists.
- Add a reusable hover hint to every button, entry, combobox, and merge-mode
  radio control. Hints explain what the control does and when to use it.
- Add a **Help** button. Its scrollable help view explains this flow:
  1. Select or browse to the game folder.
  2. Choose primary and secondary languages and a merge mode.
  3. Generate a preview and inspect target paths, hashes, and unmatched entries.
  4. Install, modify, or uninstall after reviewing the confirmation details.
- The help text explains that Install creates original backups and Uninstall
  restores them; it also distinguishes Dialogue-only keyless merging from
  Full-text merging.

## Standalone executable

- Produce a single-file, windowed Windows executable using PyInstaller's one-file
  mode. Users should not need a separate Python installation.
- Add a checked-in spec/build recipe and README instructions that state the
  supported build environment and output path, expected to be
  `dist/W3DualSubtitleManager.exe`.
- Package the app and standard-library/Tk runtime only. Do not package game
  resources, user backups, the optional external converter, or the WolvenKit
  helper. The built-in codec remains the default; users may still select an
  external converter at runtime.
- Ensure frozen-app configuration and logs continue to use the existing
  per-user application-data location, not the temporary extraction directory or
  game folder.

## Acceptance criteria

- The index-build action and its status/progress UI are absent, and startup does
  not load dialogue-index state.
- Dialogue-only merge is the leftmost and default mode. New dialogue generation,
  install, modify, and uninstall do not require an index helper or index digest.
- Existing manifests and backups remain readable, and an old active install can
  still be safely uninstalled when the index helper is unavailable. Legacy
  classifier digests are retained when parsing but do not trigger index reads.
- Startup and folder/language selection do not perform full inventory round trips.
  Generation performs one semantic compatibility pass for either codec path
  before publishing outputs.
- Long generation and file lifecycle operations display phase-specific completed
  counts that correspond to actual completed work. No fake time-based percentage
  is shown.
- The initial window and preview fit more closely to their content, while longer
  path lists remain accessible through scrolling and window resizing.
- Every interactive button and selection/edit field has a hover hint; Help
  describes the normal operation flow and backup behavior.
- The build recipe creates a single Windows executable that starts without a
  separately installed Python runtime and keeps user state outside the game
  folder.

## Scope and migration

This change does not alter the keyless dialogue heuristic, punctuation rules,
resource pairing, backup format, or transaction semantics. It does not remove
the separate WolvenKit files from source control. Existing generated previews
whose app version predates the implementation's version change may need
regeneration; existing active installs remain uninstallable through their saved
manifest and backups.

## Verification

Review every progress-emitting path for accurate totals and UI-thread-only widget
updates. Build the one-file executable using the checked-in recipe and confirm
the output does not contain game data, backups, or optional helper/converter
binaries. Check the app's core generation and install lifecycle against isolated
game fixtures; do not install to the live game during implementation.
