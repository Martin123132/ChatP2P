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
    [switch]$Json,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$script:maintenanceJson = $null
$script:maintenanceReport = $null

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
        [string[]]$CommandArgs,
        [switch]$AllowFailure
    )
    $reportMode = if ($AllowFailure) { "report_only" } else { "strict" }
    $stepReport = $null
    if ($null -ne $script:maintenanceReport) {
        $stepReport = [ordered]@{
            label = $Name
            command = @($Python) + $CommandArgs
            returncode = 0
            status = "pass"
            report_mode = $reportMode
        }
        $script:maintenanceReport.steps = @($script:maintenanceReport.steps) + $stepReport
    }

    & $Python @CommandArgs
    $returnCode = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
    if ($null -ne $stepReport) {
        $stepReport.returncode = $returnCode
    }
    if ($returnCode -ne 0) {
        if ($AllowFailure) {
            if ($null -ne $stepReport) {
                $stepReport.status = "fail"
                $stepReport.error = "$Name completed with exit code $returnCode (non-blocking report step)"
                $script:maintenanceReport.status = "fail"
            }
            Write-Warning "$Name returned exit code $returnCode (continuing maintenance loop for report review)."
            return $returnCode
        }
        if ($null -ne $stepReport) {
            $stepReport.status = "fail"
            $stepReport.error = "$Name failed with exit code $returnCode"
            $script:maintenanceReport.status = "fail"
        }
        throw "$Name failed with exit code $returnCode"
    }
    return 0
}

function Write-MaintenanceReport {
    if ($null -eq $script:maintenanceReport -or [string]::IsNullOrWhiteSpace($script:maintenanceJson)) {
        return
    }
    $json = $script:maintenanceReport | ConvertTo-Json -Depth 20
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($script:maintenanceJson, $json, $utf8NoBom)
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
    $dailyCheckJson = Join-Path $dailyCheckDir "daily-check.json"
    $actionQueueJson = Join-Path $dailyCheckDir "action-queue.json"
    $consoleJson = Join-Path $consoleDir "operator-console.json"
    $selfHealJson = Join-Path $selfHealDir "operator-self-heal-report.json"
    $actionRunJson = Join-Path $outRoot "operator-action-run-report.json"
    $maintenanceJson = Join-Path $outRoot "operator-maintenance-report.json"

    New-Item -ItemType Directory -Force -Path $outRoot, $dailyCheckDir, $consoleDir, $selfHealDir | Out-Null
    $script:maintenanceJson = $maintenanceJson
    $script:maintenanceReport = [ordered]@{
        schema = "chatp2p.operator-maintenance-report.v1"
        status = "pass"
        generated_at = (Get-Date).ToUniversalTime().ToString("o")
        config = [ordered]@{
            repo = $repoRoot
            home = $meshHomePath
            primary_invite = $PrimaryInvite
            backup_invite = if ([string]::IsNullOrWhiteSpace($BackupInvite)) { $null } else { $BackupInvite }
            out_dir = $outRoot
            reliability_dir = $reliabilityPath
            expected_primary_worker_id = if ([string]::IsNullOrWhiteSpace($ExpectedPrimaryWorkerId)) { $null } else { $ExpectedPrimaryWorkerId }
            expected_backup_worker_id = if ([string]::IsNullOrWhiteSpace($ExpectedBackupWorkerId)) { $null } else { $ExpectedBackupWorkerId }
            skip_network_checks = [bool]$SkipNetworkChecks
            partner_report = @($PartnerReport)
        }
        artifacts = [ordered]@{
            daily_check_json = $dailyCheckJson
            console_json = $consoleJson
            action_queue_json = $actionQueueJson
            self_heal_json = $selfHealJson
            action_run_json = $actionRunJson
            maintenance_json = $maintenanceJson
        }
        steps = @()
    }

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
    $consoleExitCode = Invoke-Command-Strict -Name "operator console" -CommandArgs $consoleArgs -AllowFailure

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
    $dailyCheckExitCode = Invoke-Command-Strict -Name "operator daily-check" -CommandArgs $dailyArgs -AllowFailure

    Write-Host "[3/4] rebuild action-queue..."
    if (-not (Test-Path $dailyCheckJson)) {
        throw "daily-check.json not found after daily-check: $dailyCheckJson"
    }
    $queueArgs = @(
        "-m", "chatp2p.cli", "operator", "action-queue",
        "--daily-report", $dailyCheckJson,
        "--out", $dailyCheckDir,
        "--json"
    )
    $actionQueueExitCode = Invoke-Command-Strict -Name "operator action-queue" -CommandArgs $queueArgs -AllowFailure

    Write-Host "[4/4] operator self-heal..."
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
    $selfHealExitCode = Invoke-Command-Strict -Name "operator self-heal" -CommandArgs $selfHealArgs -AllowFailure

    $action = $null
    if (Test-Path $actionQueueJson) {
        $actionQueue = Get-Content $actionQueueJson -Raw | ConvertFrom-Json
        if ($actionQueue.next_action) { $action = $actionQueue.next_action }
    }

    $consoleReport = Get-Content $consoleJson -Raw | ConvertFrom-Json
    $selfHealReport = Get-Content $selfHealJson -Raw | ConvertFrom-Json
    $repairableIssueCount = $selfHealReport.summary.repairable_issue_count
    Write-Host "`nOperator maintenance complete."
    Write-Host "Can continue without partner: $($consoleReport.summary.can_continue_without_partner)"
    Write-Host "Recommended next action:  $($consoleReport.summary.recommended_next_action)"
    Write-Host "Self-heal summary:        $repairableIssueCount repairable issue(s)"

    if ($action) {
        Write-Host "Top queue action:         $($action.action_id) (partner_required=$($action.partner_required))"
        $safeActionMessage = if ($action.can_run_without_partner) {
            "safe to dry-run locally"
        } else {
            "requires partner to act"
        }
        Write-Host "Run preview:              $safeActionMessage"
        if ($action.partner_required -or -not $action.can_run_without_partner) {
            $topActionStatus = "not_local_executable"
        } elseif (-not $action.suggested_commands) {
            $topActionStatus = "missing_commands"
        } else {
            $topActionStatus = "safe_local"
        }
        Write-Host "Top action status:        $topActionStatus"
    } else {
        $topActionStatus = "none"
    }

    if ($PreviewTopAction -and $topActionStatus -ne "safe_local") {
        if ($action) {
            Write-Warning "Skipping preview because top action cannot be run locally."
        } else {
            throw "run-top-action preview requested but no executable top action is available."
        }
    }

    $script:maintenanceReport["summary"] = [ordered]@{
        can_continue_without_partner = $consoleReport.summary.can_continue_without_partner
        recommended_next_action = $consoleReport.summary.recommended_next_action
        top_action = $action
        top_action_status = $topActionStatus
        top_action_partner_required = if ($action) { $action.partner_required } else { $null }
        repairable_issue_count = $repairableIssueCount
    }
    Write-MaintenanceReport

    if ($action -and $PreviewTopAction -and $topActionStatus -eq "safe_local") {
        Write-Host "`nPreparing preview..."
        $runActionArgs = @(
            "-m", "chatp2p.cli", "operator", "run-action",
            "--queue", $actionQueueJson,
            "--out", $actionRunJson,
            "--json"
        )
        if ($action.action_id) {
            $runActionArgs += @("--action", $action.action_id)
        }
            Invoke-Command-Strict -Name "operator run-action --dry-run" -CommandArgs $runActionArgs
    }

    if ($RunTopAction -and $action -and $topActionStatus -eq "safe_local") {
        if (-not $AllowExecute) {
            Write-Warning "RunTopAction is set, but execution is disabled. Add -AllowExecute to run this local action."
        } elseif ($PSCmdlet.ShouldProcess($action.action_id, "operator run-action --execute")) {
            Write-Host "`nRunning top local action now (allowed in operator V1)..."
            $runActionArgs = @(
                "-m", "chatp2p.cli", "operator", "run-action",
                "--queue", $actionQueueJson,
                "--out", $actionRunJson,
                "--execute",
                "--json"
            )
            if ($action.action_id) {
                $runActionArgs += @("--action", $action.action_id)
            }
            Invoke-Command-Strict -Name "operator run-action --execute" -CommandArgs $runActionArgs
        }
    } elseif ($RunTopAction) {
        throw "run-top-action requested, but top action is not safe for local execute. Regenerate the queue and resolve partner-required items first."
    }
    Write-MaintenanceReport
    if ($Json) {
        $script:maintenanceReport | ConvertTo-Json -Depth 20
    }
}
catch {
    if ($null -ne $script:maintenanceReport) {
        $script:maintenanceReport.status = "fail"
        $script:maintenanceReport["error"] = "$_"
        Write-MaintenanceReport
    }
    Write-Error $_
    exit 1
}
