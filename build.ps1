$ErrorActionPreference = 'Stop'

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'Build the Windows executable on Windows.'
}

Push-Location $PSScriptRoot
try {
    python -m PyInstaller --clean --noconfirm w3sub.spec
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    Pop-Location
}
