param(
    [Parameter(Mandatory = $true)][string]$Dir,
    [string]$Out = ""
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Dir)) {
    throw "Directory not found: $Dir"
}
if (-not $Out) {
    $Out = Join-Path $Dir "output.mp4"
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    throw "ffmpeg not found in PATH"
}

$local = Join-Path $Dir "local.m3u8"

$numbered = @(Get-ChildItem -LiteralPath $Dir -Filter "*.ts" -File |
    Where-Object { $_.BaseName -match '^\d+$' })

if ($numbered.Count -gt 0) {
    $sorted = $numbered | Sort-Object { [int64]$_.BaseName }
    $numbers = @($sorted | ForEach-Object { [int64]$_.BaseName })
    $min = $numbers[0]
    $max = $numbers[-1]
    $missing = @()
    if (($max - $min + 1) -gt $numbers.Count) {
        $seen = @{}
        foreach ($n in $numbers) { $seen[$n] = $true }
        for ($n = $min; $n -le $max; $n++) {
            if (-not $seen.ContainsKey($n)) { $missing += $n }
        }
        Write-Warning ("Missing {0} segment(s): {1}{2}" -f $missing.Count,
            (($missing | Select-Object -First 10) -join ", "),
            $(if ($missing.Count -gt 10) { " ..." } else { "" }))
    }

    $list = Join-Path $Dir "concat.txt"
    $sorted | ForEach-Object { "file '$($_.Name)'" } |
        Set-Content -LiteralPath $list -Encoding ASCII
    Write-Host "Concatenating $($sorted.Count) segments..."
    & ffmpeg -y -hide_banner -loglevel warning -stats -fflags +genpts `
        -f concat -safe 0 -i $list -c copy $Out
    $code = $LASTEXITCODE
    Remove-Item -LiteralPath $list -Force
    if ($code -ne 0) {
        throw "ffmpeg concat failed (exit $code)"
    }
}
elseif (Test-Path -LiteralPath $local) {
    Write-Warning "No numbered .ts files found, falling back to local.m3u8"
    & ffmpeg -y -hide_banner -loglevel warning -stats `
        -allowed_extensions ALL -protocol_whitelist "file,crypto,data" `
        -i $local -c copy $Out
    if ($LASTEXITCODE -ne 0) { throw "ffmpeg failed (exit $LASTEXITCODE)" }
}
else {
    $parts = @(Get-ChildItem -LiteralPath $Dir -Filter "*.ts" -File | Sort-Object Name)
    if ($parts.Count -eq 0) {
        throw "No .ts files or local.m3u8 found in $Dir"
    }
    Write-Warning "No numbered segments or playlist, using filename order"
    $outTs = Join-Path $Dir "output.ts"
    $fs = [IO.File]::Create($outTs)
    try {
        foreach ($part in $parts) {
            $input = [IO.File]::OpenRead($part.FullName)
            try { $input.CopyTo($fs) } finally { $input.Dispose() }
        }
    }
    finally {
        $fs.Dispose()
    }
    & ffmpeg -y -hide_banner -loglevel warning -stats -i $outTs -c copy $Out
    $code = $LASTEXITCODE
    Remove-Item -LiteralPath $outTs -Force
    if ($code -ne 0) { throw "ffmpeg remux failed (exit $code)" }
}

Write-Host "Output: $Out"
