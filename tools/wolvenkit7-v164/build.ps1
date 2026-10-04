param(
    [string]$Repository = 'https://github.com/WolvenKit/WolvenKit-7.git',
    [string]$SourceArchive,
    [string]$OutputRoot = $PSScriptRoot
)
$ErrorActionPreference = 'Stop'
$commit = '8eb4026349b1e419c189d42b8286b4a5613ab433'
$taskSdkVersion = (& dotnet --version).Trim()
if ($taskSdkVersion -ne '10.0.300') { throw 'Reproducible helper build requires .NET SDK 10.0.300' }
$version = 'w3sub-wolvenkit7-v164-1'
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
$taskBuildRoot = Join-Path ([IO.Path]::GetTempPath()) ('w3sub-helper-build-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $taskBuildRoot, $OutputRoot -Force | Out-Null
$source = Join-Path $taskBuildRoot 'source'
function Run-Native([string]$Command, [string[]]$Arguments) {
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Command failed with exit code $LASTEXITCODE" }
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
if ($SourceArchive) {
    # Rebuild from the corresponding source package distributed alongside bin.
    $archiveInput = [IO.Path]::GetFullPath($SourceArchive)
    if ($archiveInput.EndsWith('.001')) {
        $archiveInput = Join-Path $taskBuildRoot 'corresponding-source.zip'
        $joined = [IO.File]::Create($archiveInput)
        try {
            $partNumber = 1
            do {
                $partPath = $SourceArchive.Substring(0, $SourceArchive.Length - 3) + $partNumber.ToString('000')
                if (!(Test-Path -LiteralPath $partPath)) { break }
                $partStream = [IO.File]::OpenRead([IO.Path]::GetFullPath($partPath))
                try { $partStream.CopyTo($joined) } finally { $partStream.Dispose() }
                $partNumber++
            } while ($true)
        } finally { $joined.Dispose() }
    }
    [IO.Compression.ZipFile]::ExtractToDirectory($archiveInput, $taskBuildRoot)
} else {
    Run-Native git @('clone', '--no-checkout', $Repository, $source)
    Run-Native git @('-C', $source, 'checkout', '--detach', $commit)
    $actual = (& git -C $source rev-parse HEAD).Trim()
    if ($actual -ne $commit) { throw 'Pinned source checkout mismatch' }
    Run-Native git @('-C', $source, 'apply', '--check', (Join-Path $PSScriptRoot 'patches/wolvenkit7-v164-batch.patch'))
    Run-Native git @('-C', $source, 'apply', (Join-Path $PSScriptRoot 'patches/wolvenkit7-v164-batch.patch'))
}
$staging = Join-Path $taskBuildRoot 'runtime'
Run-Native dotnet @('build', (Join-Path $source 'WolvenKit.CLI/WolvenKit.CLI.csproj'), '-c', 'Release', '-p:Platform=x64', '-p:Deterministic=true', "-p:SourceRevisionId=$commit", "-p:PathMap=$source=/_/wolvenkit7", '-o', $staging, '--verbosity', 'minimal')
# Keep assemblies/configuration and satellite resources, excluding debug symbols
# and the unrelated upstream DDS converter copied by WolvenKit.Common.
$bin = Join-Path $OutputRoot 'bin'
if (Test-Path -LiteralPath $bin) {
    $resolvedBin = (Resolve-Path -LiteralPath $bin).Path
    if ($resolvedBin -ne (Join-Path $OutputRoot 'bin')) { throw 'Unexpected output directory' }
    Remove-Item -LiteralPath $resolvedBin -Recurse -Force
}
New-Item -ItemType Directory -Path $bin | Out-Null
foreach ($file in Get-ChildItem -LiteralPath $staging -Recurse -File) {
    $relative = [IO.Path]::GetRelativePath($staging, $file.FullName)
    if ($relative.StartsWith('Tools\') -or $file.Extension -notin @('.dll', '.exe', '.config')) { continue }
    $target = Join-Path $bin $relative
    New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
    Copy-Item -LiteralPath $file.FullName -Destination $target
}
$assets = Get-Content -LiteralPath (Join-Path $source 'WolvenKit.CLI/obj/project.assets.json') -Raw | ConvertFrom-Json -AsHashtable
$packageRoot = @($assets.packageFolders.Keys)[0]
$notices = Join-Path $OutputRoot 'third-party'
New-Item -ItemType Directory -Path $notices -Force | Out-Null
# Preserve reviewed licenses fetched from upstream when NuGet supplies only URLs.
$reviewedNotices = Join-Path $PSScriptRoot 'third-party'
if ((Test-Path -LiteralPath $reviewedNotices) -and
    ([IO.Path]::GetFullPath($reviewedNotices) -ne [IO.Path]::GetFullPath($notices))) {
    Copy-Item -LiteralPath $reviewedNotices -Destination $OutputRoot -Recurse -Force
}
$dependencies = @()
foreach ($entry in $assets.libraries.GetEnumerator() | Sort-Object Key) {
    if ($entry.Value.type -ne 'package') { continue }
    $packagePath = Join-Path $packageRoot $entry.Value.path
    $nuspec = Get-ChildItem -LiteralPath $packagePath -Filter '*.nuspec' | Select-Object -First 1
    [xml]$xml = Get-Content -LiteralPath $nuspec.FullName -Raw
    $metadata = $xml.package.metadata
    $id = [string]$metadata.id
    $packageVersion = [string]$metadata.version
    $license = [string]$metadata.license.'#text'
    $packageNoticeRoot = Join-Path $notices ($id + '-' + $packageVersion)
    New-Item -ItemType Directory -Path $packageNoticeRoot -Force | Out-Null
    Copy-Item -LiteralPath $nuspec.FullName -Destination $packageNoticeRoot
    if ($metadata.license.type -eq 'file') {
        Copy-Item -LiteralPath (Join-Path $packagePath $license) -Destination $packageNoticeRoot
    }
    foreach ($notice in Get-ChildItem -LiteralPath $packagePath -File -Recurse | Where-Object { $_.Name -match '^(LICENSE|NOTICE|THIRD.PARTY)' }) {
        # Preserve package license/notice paths rather than collapsing filenames.
        $relative = [IO.Path]::GetRelativePath($packagePath, $notice.FullName)
        $target = Join-Path $packageNoticeRoot $relative
        New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
        Copy-Item -LiteralPath $notice.FullName -Destination $target -Force
    }
    if (!(Get-ChildItem -LiteralPath $packageNoticeRoot -Recurse -File | Where-Object { $_.Name -match '^LICENSE' -or $_.Name -eq 'License.md' })) {
        throw "No reviewed license text for $id $packageVersion; cannot distribute this build"
    }
    $dependencies += [ordered]@{ id=$id; version=$packageVersion; sha512=$entry.Value.sha512;
        license=$license; license_type=[string]$metadata.license.type;
        license_url=[string]$metadata.licenseUrl; homepage=[string]$metadata.projectUrl;
        repository=[string]$metadata.repository.url }
}
$dependencies | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $OutputRoot 'dependencies.json') -Encoding utf8NoBOM
$sourceName = 'wolvenkit7-v164-corresponding-source.zip'
$sourcePackage = Join-Path $OutputRoot $sourceName
if ($SourceArchive) {
    Copy-Item -LiteralPath $archiveInput -Destination $sourcePackage -Force
} else {
    Run-Native git @('-C', $source, 'archive', '--format=zip', '--prefix=source/', "--output=$sourcePackage", $commit)
    $archive = [IO.Compression.ZipFile]::Open($sourcePackage, [IO.Compression.ZipArchiveMode]::Update)
    try {
        # Overlay exactly the patched build sources; unchanged files remain the pinned archive.
        foreach ($relative in @('WolvenKit.CR2W/CR2W/CR2WFile.cs', 'WolvenKit.CLI/Program.cs', 'WolvenKit.CLI/ReferenceBatch.cs')) {
            $old = $archive.GetEntry('source/' + $relative)
            if ($old) { $old.Delete() }
            [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, (Join-Path $source $relative), 'source/' + $relative, [IO.Compression.CompressionLevel]::Optimal) | Out-Null
        }
        foreach ($relative in @('build.ps1', 'patches/wolvenkit7-v164-batch.patch', 'LICENSE', 'README.md', 'THIRD_PARTY_NOTICES.md')) {
            [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, (Join-Path $PSScriptRoot $relative), $relative, [IO.Compression.CompressionLevel]::Optimal) | Out-Null
        }
        foreach ($file in @(Get-Item -LiteralPath (Join-Path $OutputRoot 'dependencies.json')) + @(Get-ChildItem -LiteralPath $notices -Recurse -File)) {
            $relative = [IO.Path]::GetRelativePath($OutputRoot, $file.FullName).Replace('\','/')
            [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $file.FullName, $relative, [IO.Compression.CompressionLevel]::Optimal) | Out-Null
        }
    } finally { $archive.Dispose() }
}
# Split below GitHub's file limit while preserving the complete ZIP byte stream.
$sourceParts = @()
$inputStream = [IO.File]::OpenRead($sourcePackage)
try {
    $partNumber = 1
    $buffer = New-Object byte[] (1024 * 1024)
    while ($inputStream.Position -lt $inputStream.Length) {
        $partName = $sourceName + '.' + $partNumber.ToString('000')
        $sourceParts += $partName
        $partStream = [IO.File]::Create((Join-Path $OutputRoot $partName))
        try {
            $remaining = 64 * 1024 * 1024
            while ($remaining -gt 0 -and ($count = $inputStream.Read($buffer, 0, [Math]::Min($buffer.Length, $remaining))) -gt 0) {
                $partStream.Write($buffer, 0, $count)
                $remaining -= $count
            }
        } finally { $partStream.Dispose() }
        $partNumber++
    }
} finally { $inputStream.Dispose() }
Remove-Item -LiteralPath $sourcePackage
$hashes = [ordered]@{}
foreach ($file in Get-ChildItem -LiteralPath $bin -Recurse -File | Sort-Object FullName) {
    $relative = [IO.Path]::GetRelativePath($OutputRoot, $file.FullName).Replace('\','/')
    $hashes[$relative] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
}
foreach ($partName in $sourceParts) {
    $hashes[$partName] = (Get-FileHash -LiteralPath (Join-Path $OutputRoot $partName) -Algorithm SHA256).Hash.ToLowerInvariant()
}
$metadata = [ordered]@{ schema=1; version=$version; upstream_commit=$commit; supports_v164=$true;
    source_packages=$sourceParts; files=$hashes }
$manifestPath = Join-Path $OutputRoot 'helper-manifest.json'
$metadata | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $manifestPath -Encoding utf8NoBOM
Write-Output ('Manifest SHA256 (pin in the Python adapter after reviewing the build): ' + (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant())
Write-Output "Build workspace retained for inspection: $taskBuildRoot"
