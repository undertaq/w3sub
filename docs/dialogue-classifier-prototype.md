# Dialogue classifier prototype

## Result

Missing localization-key associations are useful as a candidate pool, but they
are not a dialogue label. On the installed Remastered 5.00 `content0` English
and Chinese pair, the scan found **79,790 shared IDs without any key
association**. Parsing the successfully decoded packed scenes found **64,832**
of those IDs in spoken `CStorySceneLine.dialogLine` fields (81.3%). Another **7,145** occur
in `CStorySceneChoiceLine.choiceLine`, which is conversational choice UI and
should be classified separately from spoken subtitles. The remaining **7,813**
keyless IDs had no reference in either field among the successfully decoded
scenes. One resource failed parsing, so those absence counts may be slightly
high. None of those groups should be inferred from keylessness alone.

## Partial 5.00 scene sample

- 31 `content0` bundles contained 6,380 physical `.w2scene` entries and 6,222
  unique depot paths; 136 paths appeared more than once.
- All 6,380 scene payloads passed the bundle size and CRC checks. The independent
  current Witcher 3 Blender Tools CR2W reader parsed 6,379; one failed with a
  `readUShortCheck() missing 1 required positional argument: 'pos'` parser
  exception.
- The parser found 65,639 distinct spoken-line IDs and 7,408 distinct choice
  IDs. Of these, 64,832 spoken-line IDs and 7,145 choice IDs were present in
  both the installed `en.w3strings` and `zh.w3strings`; all 71,977 are in the
  shared keyless set. Another 1,070 spoken/choice IDs were not shared by that
  language pair.

## WolvenKit 7 conversion probe

The released WolvenKit 7.2 CLI initially rejected the extracted scene with
`UnsupportedVersion`, because its CR2W reader caps the accepted version at
163. In a temporary WolvenKit source checkout, I raised only that ceiling to
164, rebuilt the CLI, and converted
`q111_traveling_merchant.w2scene` with `--cr2w2json`. WolvenKit then parsed the
scene's 45 chunks and emitted the typed subtitle fields successfully. Its JSON
contained IDs `421113`, `421115`, `421121`, `421119`, and `497853` in the two
expected fields, matching the independent CR2W reader's output for that sample.
The repository's released WolvenKit binaries and the game installation were
not changed.

The scene corpus counts above came from the independent parser, not from 6,380
WolvenKit CLI launches. The integration now bundles a pinned, patched separate
WolvenKit helper under `tools/wolvenkit7-v164/` with its license and complete
corresponding source. `w3sub_app/wolvenkit_scene_refs.py` validates its identity
and accepts batch typed references, including other LocalizedString fields;
unknown/opaque payload coverage is an explicit failure.

## Production inventory and current result

The subsequent read-only census of the live 5.00 installation inventoried **31
active bundles with 365,866 physical entries**, including the 6,380 scenes,
plus **1,581 loose files**. No active DLC directory is present in that local
installation. The builder also includes active DLC when present; the absence
of local DLC is not evidence that DLC parsing has been verified on this game.

The production build returned **no index** and published **no cache**. Its
first diagnostic was `content/content0/ar.w3strings: unaudited loose resource
format; bundle inventory alone is incomplete`. It stopped before parsing
payloads: verified resources = 0, parsed resources = 0. These inventory counts
must not be presented as successful parser coverage.

The loose files include 1,527 `.ws` scripts and 18 `.w3strings` files, plus
other formats. Literal `GetLocStringByKeyExt` calls in `actor.ws` and
`inventoryComponent.ws` confirm non-CR2W localization references. Such usage
needs a key-resolution audit and OTHER classification; unresolved/dynamic
keys must continue failing closed. Excluding `.w3strings` as text sinks would
remove the first blocker, but would not establish complete coverage.

The bundle census also contains 113,009 `.buffer`, 49 `.menu`, 1 `.hud`,
1 `.guiconfig`, 155 `.csv`, 462 `.xml`, and 1,122 `.subs` entries. These formats
remain unaudited for reference coverage. Their full counts and failure
diagnostics are written to the per-game index status under application data.
No game resources were changed by this audit.

## What this establishes, and what it does not

The appropriate initial rule for subtitle mode is **explicit
`CStorySceneLine.dialogLine` reference AND presence in both selected language
resources**. Keep `choiceLine` as its own optional context because the game
renders those strings as interaction choices. Keylessness alone would include
many other records. Typed fields distinguish spoken subtitles, choice UI, and
unrelated scene metadata without relying on string length, punctuation, or
language wording.

This prototype does not yet establish that a string ID referenced by a spoken
scene line is never reused by a menu, item, overhead-text, or objective
resource. Before enabling automatic merges, the index builder must scan the other
structured reference types, mark mixed-use IDs ambiguous, fingerprint the
complete bundle inventory, and bind the index to the detected game resources.
The index/cache/merge pipeline is now integrated, with an explicit GUI
Build/Refresh action, worker progress, version and resource freshness checks,
and path-specific failures. Its production index remains unavailable pending
the resource-format coverage audits above, so Dialogue only remains disabled
on the live installation. Full text remains available.

The native merger now has an ID-only path for strings present in both selected
language files with no key associations in either, but only when a complete
current index proves exclusively spoken-subtitle usage. Keyed strings still
need a shared key and that same exclusive context. Choice UI and mixed-use or
unknown contexts stay primary-only. The external CSV path has no keyless
fallback. This implementation rule is covered by fixtures; the partial scene
sample cannot authorize a production merge.
