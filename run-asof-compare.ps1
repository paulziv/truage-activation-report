# run-asof-compare.ps1
# Pulls ONE fresh HubSpot snapshot, then compares TruAge KPI metrics as-of
# 2026-06-29, 2026-06-30, and 2026-07-01 against that same snapshot.
#
# Prereqs:
#   - $env:HUBSPOT_TOKEN set to a read-only HubSpot private-app token
#     (scopes: crm.objects.deals.read, crm.schemas.deals.read,
#      crm.objects.custom.read, crm.schemas.custom.read)
#   - Python 3 with `requests` installed
#
# This does NOT replay true historical HubSpot state — see the caveat at
# the top of compare_asof.py. It evaluates today's deal data as of each date.

$repoPath = "C:\Users\paulz\dev\truage-activity-report"
Set-Location $repoPath

if ($null -eq $env:HUBSPOT_TOKEN) {
    Write-Output "HUBSPOT_TOKEN is not set in this session."
    Write-Output "Set it first, e.g.: `$env:HUBSPOT_TOKEN = 'pat-na1-...'"
    exit 1
}

$pullFile = "hubspot_pull_$(Get-Date -Format 'yyyy-MM-dd').json"

Write-Output "Pulling fresh HubSpot data -> $($pullFile)..."
python fetch_from_hubspot.py --output $pullFile

if ($LASTEXITCODE -ne 0) {
    Write-Output "Fetch failed — see error above."
    exit 1
}

Write-Output "Comparing as-of 2026-06-29, 2026-06-30, 2026-07-01..."
python compare_asof.py --input $pullFile --dates 2026-06-29 2026-06-30 2026-07-01
