$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$toolsRoot = Join-Path $projectRoot '.tools'
$archive = Join-Path $toolsRoot 'ffmpeg.zip'
$destination = Join-Path $toolsRoot 'ffmpeg'
New-Item -ItemType Directory -Path $toolsRoot -Force | Out-Null
# Windows build linked by ffmpeg.org. Pin the inspected 9.0.2 archive.
$expectedHash = '60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba'
Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile $archive
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expectedHash) {
    throw 'The upstream archive changed. Verify the new release and update the pin before installing.'
}
Expand-Archive -LiteralPath $archive -DestinationPath $destination -Force
$binary = Join-Path $destination 'ffmpeg-9.0.2-essentials_build/bin/ffmpeg.exe'
if (-not (Test-Path -LiteralPath $binary)) { throw 'Expected FFmpeg version was not found' }
& $binary -version | Select-Object -First 1
