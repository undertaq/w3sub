# WolvenKit 7 v164 reference helper

Separate Windows x64 process, based on WolvenKit/WolvenKit-7 commit
`8eb4026349b1e419c189d42b8286b4a5613ab433`, with GPLv3 v164 and batch patches.
Requires Windows .NET Framework 4.8.1. Python does not link the assemblies.

Ship this entire directory together. `bin/`, the hash manifest, patch, notices,
resolved dependency manifest, and `wolvenkit7-v164-corresponding-source.zip.001` / `.002`
form one distribution. The ZIP is split into numbered 64 MiB parts; the recipe joins them automatically. Never distribute the binaries without the matching
source archive in the same download/release. The archive contains the complete
pinned upstream source, exact patched build sources, recipe, patch, dependencies
and notices. Game resources and localized game text are excluded.

Build with PowerShell 7, Git, .NET SDK 10.0.300 and NuGet access:

```powershell
./build.ps1
# Rebuild from the shipped corresponding source (NuGet restores exact versions):
./build.ps1 -SourceArchive ./wolvenkit7-v164-corresponding-source.zip.001 -OutputRoot ./rebuilt
```

The version and commit are fixed. The build enables deterministic compilation
and maps temporary source paths. NuGet versions and package integrity hashes
are recorded in `dependencies.json`. Any replacement build needs review and a
new trusted manifest SHA256 in the Python adapter before it is accepted.

Protocol: `--w3sub-identity` emits schema/version/commit/v164 support.
`--w3sub-batch MANIFEST` reads a UTF-8 JSON object:

```json
{"schema":1,"resources":[{"resource_identity":"bundle:entry","path":"C:\\scratch\\scene.w2scene"}]}
```

Exactly one JSONL record is emitted per input resource. Success contains
`schema`, `resource_identity`, `ok: true`, and `references`, each with
`string_id`, `owner_type`, `field_name`. Failure contains `ok: false` and
`error`; any failure exits nonzero. There is no partial success contract.

Every typed LocalizedString is retained, including nested fields and arrays.
Owner means the nearest structured type that owns the field; array elements
retain the containing owner/field. Opaque unknown bytes, additional bytes,
uninterpreted embedded payloads/buffers, unknown types, and malformed references
fail closed. The helper does not establish inventory coverage by itself. A full
production index must prove all reference-bearing resource formats are included.
Only numeric IDs/types/field names are emitted. Parsing diagnostics do not go to
stdout. No conversion or installation is performed by the batch interface.

Dependency disposition: the pinned `System.Text.Json` 7.0.1 is in the affected
range of [GHSA-hh2w-p6rv-4g7w](https://github.com/advisories/GHSA-hh2w-p6rv-4g7w)
(CVE-2024-30105), a denial of service involving
`JsonSerializer.DeserializeAsyncEnumerable` on untrusted input. The added batch
protocol parses its manifest with Newtonsoft `JObject.Parse`; the reviewed
batch path did not establish reachability of the affected method. This is not
a reachability proof. Complete an audited dependency update or a reachability
proof before expanding helper exposure. Any dependency update must rebuild
from the pinned source recipe, ship the matching corresponding source and
notices, and update the reviewed dependency hashes and trusted helper manifest.
