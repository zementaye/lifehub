# Regenerates COMMIT_HISTORY.md from the real git log, newest first.
# Run this from anywhere inside the repo after pushing new commits:
#
#   .\scripts\Update-CommitHistory.ps1
#
# Then review and commit the updated file like any other change:
#
#   git add COMMIT_HISTORY.md
#   git commit -m "Update commit history"
#   git push

$ErrorActionPreference = "Stop"

$repoRoot = git rev-parse --show-toplevel
if (-not $repoRoot) {
    throw "Not inside a git repository."
}

$outFile = Join-Path $repoRoot "COMMIT_HISTORY.md"
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm"

$headerLines = @(
    "# Commit History",
    "",
    "Auto-generated from git log - do not hand-edit. Regenerate with:",
    "",
    '```powershell',
    '.\scripts\Update-CommitHistory.ps1',
    '```',
    "",
    "Last updated: $timestamp",
    "",
    "---",
    ""
)

$log = git log --date=short --pretty=format:"- **%ad** `%h` - %s (%an)"

($headerLines + $log) | Set-Content -Path $outFile -Encoding utf8

Write-Host "Wrote $outFile"
