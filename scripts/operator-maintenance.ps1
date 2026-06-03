<# 
Offline-friendly operator maintenance helper for ChatP2P.

Usage:
  .\scripts\operator-maintenance.ps1 `
    -Root C:\Projects\ChatP2P `
    -PrimaryInvite C:\ChatP2PData\alpha-invite.json `
    -BackupInvite C:\ChatP2PData\backup-alpha-invite.json `
    -OutRoot C:\ChatP2PData\maintenance
#>

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Root = (Get-Location).Path,
    [Parameter(Mandatory = $true)]
    [string]$PrimaryInvite,
    [string]$BackupInvite = "",
    [string]$OutRoot = (Join-Path (Get-Location).Path "ChatP2PData/maintenance"),
    [string]$MeshHome = "",
    [string]$ReliabilityDir = "",
    [switch]$SkipNetworkChecks,
    [string]$ExpectedPrimaryWorkerId = "",
    [string]$ExpectedBackupWorkerId = "",
    [string[]]$PartnerReport = @(),
    [switch]$PreviewTopAction,
    [switch]$RunTopAction,
    [switch]$AllowExecute,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

function Resolve-PathOrDefault {
    param([string]$Path, [string]$Fallback, [string]$Label)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        if ([string]::IsNullOrWhiteSpace($Fallback)) {
            throw "$Label path is required and no fallback was provided."
        }
        $Path = $Fallback
    }
    $candidate = [System.IO.Path]::GetFullPath($Path)
    return $candidate
}

function Invoke-Command-Strict {
    param(
        [string]$Name,
        [string[]]$Args
    )
    & $python @Args
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE"
    }
}

try {
    $repoRoot = Resolve-Path $Root
    if (-not $repoRoot) { throw "Root not found: $Root" }
    Set-Location $repoRoot

    $repoRoot = $repoRoot.Path
    $meshHomePath = if ([string]::IsNullOrWhiteSpace($MeshHome)) { Join-Path $repoRoot ".mesh" } else { $MeshHome }
    $outRoot = Resolve-PathOrDefault -Path $OutRoot -Fallback (Join-Path $repoRoot "ChatP2PData/maintenance") -Label "out root"
    $dailyCheckDir = Join-Path $outRoot "daily-check"
    $consoleDir = Join-Path $outRoot "operator-console"
    $selfHealDir = Join-Path $outRoot "operator-self-heal"
    $reliabilityPath = if ([string]::IsNullOrWhiteSpace($ReliabilityDir)) { Join-Path $outRoot "reliability" } else { $ReliabilityDir }

    New-Item -ItemType Directory -Force -Path $outRoot, $dailyCheckDir, $consoleDir, $selfHealDir | Out-Null

    Write-Host "[1/4] operator console (read-only)..."
    $consoleArgs = @(
        "-m", "chatp2p.cli", "operator", "console",
        "--repo", $repoRoot,
        "--home", $meshHomePath,
        "--primary-invite", $PrimaryInvite,
        "--out", $consoleDir,
        "--daily-check-dir", $dailyCheckDir,
        "--reliability-dir", $reliabilityPath,
        "--json"
    )
    if (-not [string]::IsNullOrWhiteSpace($BackupInvite)) {
        $consoleArgs += @("--backup-invite", $BackupInvite)
    }
    if (-not [string]::IsNullOrWhiteSpace($ExpectedPrimaryWorkerId)) {
        $consoleArgs += @("--expected-primary-worker-id", $ExpectedPrimaryWorkerId)
    }
    if (-not [string]::IsNullOrWhiteSpace($ExpectedBackupWorkerId)) {
        $consoleArgs += @("--expected-backup-worker-id", $ExpectedBackupWorkerId)
    }
    if ($SkipNetworkChecks) {
        $consoleArgs += "--skip-network-checks"
    }
    foreach ($report in $PartnerReport) {
        if (-not [string]::IsNullOrWhiteSpace($report)) {
            $consoleArgs += @("--partner-report", $report)
        }
    }
    Invoke-Command-Strict -Name "operator console" -Args $consoleArgs

    Write-Host "[2/4] operator daily-check..."
    $dailyArgs = @(
        "-m", "chatp2p.cli", "operator", "daily-check",
        "--repo", $repoRoot,
        "--home", $meshHomePath,
        "--primary-invite", $PrimaryInvite,
        "--out", $dailyCheckDir,
        "--console-out", $consoleDir,
        "--json"
    )
    if (-not [string]::IsNullOrWhiteSpace($BackupInvite)) {
        $dailyArgs += @("--backup-invite", $BackupInvite)
    }
    if (-not [string]::IsNullOrWhiteSpace($ExpectedPrimaryWorkerId)) {
        $dailyArgs += @("--expected-primary-worker-id", $ExpectedPrimaryWorkerId)
    }
    if (-not [string]::IsNullOrWhiteSpace($ExpectedBackupWorkerId)) {
        $dailyArgs += @("--expected-backup-worker-id", $ExpectedBackupWorkerId)
    }
    if ($SkipNetworkChecks) {
        $dailyArgs += "--skip-network-checks"
    }
    Invoke-Command-Strict -Name "operator daily-check" -Args $dailyArgs

    Write-Host "[3/4] rebuild action-queue..."
    $dailyCheckJson = Join-Path $dailyCheckDir "daily-check.json"
    if (-not (Test-Path $dailyCheckJson)) {
        throw "daily-check.json not found after daily-check: $dailyCheckJson"
    }
    $queueArgs = @(
        "-m", "chatp2p.cli", "operator", "action-queue",
        "--daily-report", $dailyCheckJson,
        "--out", $dailyCheckDir,
        "--json"
    )
    Invoke-Command-Strict -Name "operator action-queue" -Args $queueArgs

    Write-Host "[4/4] operator self-heal..."
    $actionQueueJson = Join-Path $dailyCheckDir "action-queue.json"
    $consoleJson = Join-Path $consoleDir "operator-console.json"
    if (-not (Test-Path $actionQueueJson)) {
        throw "action-queue.json not found after action-queue: $actionQueueJson"
    }
    if (-not (Test-Path $consoleJson)) {
        throw "operator-console.json not found after console: $consoleJson"
    }
    $selfHealArgs = @(
        "-m", "chatp2p.cli", "operator", "self-heal",
        "--console-report", $consoleJson,
        "--daily-report", $dailyCheckJson,
        "--action-queue", $actionQueueJson,
        "--out", $selfHealDir,
        "--json"
    )
    Invoke-Command-Strict -Name "operator self-heal" -Args $selfHealArgs

    $selfHealJson = Join-Path $selfHealDir "operator-self-heal-report.json"
    $action = $null
    if (Test-Path $actionQueueJson) {
        $actionQueue = Get-Content $actionQueueJson -Raw | ConvertFrom-Json
        if ($actionQueue.next_action) { $action = $actionQueue.next_action }
    }

    $consoleReport = Get-Content $consoleJson -Raw | ConvertFrom-Json
    Write-Host "`nOperator maintenance complete."
    Write-Host "Can continue without partner: $($consoleReport.summary.can_continue_without_partner)"
    Write-Host "Recommended next action:  $($consoleReport.summary.recommended_next_action)"
    Write-Host "Self-heal summary:        $((Get-Content $selfHealJson -Raw | ConvertFrom-Json).summary.repairable_issue_count) repairable issue(s)"

    if ($action) {
        Write-Host "Top queue action:         $($action.action_id) (partner_required=$($action.partner_required))"
        $safeActionMessage = if ($action.can_run_without_partner) {
            "safe to dry-run locally"
        } else {
            "requires partner to act"
        }
        Write-Host "Run preview:              $safeActionMessage"
    }

    if ($action -and $PreviewTopAction) {
        Write-Host "`nPreparing preview..."
        $runActionArgs = @(
            "-m", "chatp2p.cli", "operator", "run-action",
            "--queue", $actionQueueJson,
            "--out", (Join-Path $outRoot "operator-action-run-report.json"),
            "--json"
        )
        if ($action.action_id) {
            $runActionArgs += @("--action", $action.action_id)
        }
        Invoke-Command-Strict -Name "operator run-action --dry-run" -Args $runActionArgs
    }

    if ($RunTopAction -and $action -and $action.can_run_without_partner -and $action.partner_required -eq $false) {
        if (-not $AllowExecute) {
            Write-Warning "RunTopAction is set, but execution is disabled. Add -AllowExecute to run this local action."
        } elseif ($PSCmdlet.ShouldProcess($action.action_id, "operator run-action --execute")) {
            Write-Host "`nRunning top local action now (allowed in operator V1)..."
            $runActionArgs = @(
                "-m", "chatp2p.cli", "operator", "run-action",
                "--queue", $actionQueueJson,
                "--out", (Join-Path $outRoot "operator-action-run-report.json"),
                "--execute",
                "--json"
            )
            if ($action.action_id) {
                $runActionArgs += @("--action", $action.action_id)
            }
            Invoke-Command-Strict -Name "operator run-action --execute" -Args $runActionArgs
        }
    } elseif ($RunTopAction) {
        Write-Warning "Top action was not safe for local execute; run-action was not invoked."
    }
}
catch {
    Write-Error $_
    exit 1
}
