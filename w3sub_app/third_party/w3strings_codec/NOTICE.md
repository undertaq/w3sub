# W3Strings codec attribution

The Python binary codec in `w3sub_app/w3strings_native.py` adapts the container
layout, language magic table, bit6 section-count framing, rotating text
obfuscation, and sorted lookup-block writer from the MIT-licensed
[Witcher3StringEditor](https://github.com/0x7A2C9E5D/Witcher3StringEditor),
by 0x7A2C9E5D, commit `a2baff84f53241a18a2ddd776aeb1c1ec1863560`.

Audited source files: `Witcher3StringEditor.Serializers/W3Strings/`
`W3StringsReader.cs`, `W3StringsWriter.cs`, `W3StringsFormat.cs`,
`SectionCount.cs`, `StoredText.cs`, `W3StringEntry.cs`, `W3KeyEntry.cs`, and
`Witcher3StringEditor.Contracts/Language.cs`. The upstream MIT license is
included verbatim in `LICENSE`.

Local changes: standard-library Python implementation; explicit support only
for formats 162, 163, and 164; strict Unicode decoding/encoding; duplicate string
IDs and exact repeated key associations rejected; all distinct associations
retained, including references to IDs in other resources; structured merging
without CSV. `W3StringsItemReader`'s first-key collapse is deliberately unused.
No upstream .NET runtime or `w3stringsr` dependency is included or invoked.
