# ChatP2P Operator Reliability

This workflow is for the two-lane alpha setup where the operator can keep working without asking the partner to run manual commands.

It checks:

- primary coordinator lane
- backup coordinator lane
- expected worker liveness on each lane
- verified echo inference proof on both lanes
- token redaction across generated artifacts

It does not expose the raw invite token in the report files.
Deterministic failover smoke is available with `--include-deterministic-smoke`, but it is skipped by default so recurring checks do not leave single-worker deterministic jobs pending.

## One-Shot Pack

Run this from the operator machine:

```powershell
Set-Location D:\Projects\ChatP2P
$env:PYTHONPATH='D:\Projects\ChatP2P\src'

python -m chatp2p.cli operator reliability-pack `
  --primary-invite D:\ChatP2PData\alpha-invite.json `
  --backup-invite D:\ChatP2PData\backup-alpha-invite-partner.json `
  --expected-primary-worker-id worker_PRIMARY `
  --expected-backup-worker-id worker_BACKUP `
  --out D:\ChatP2PData\reliability-pack-live
```

The command writes:

- `D:\ChatP2PData\reliability-pack-live\reliability-summary.json`
- `D:\ChatP2PData\reliability-pack-live\reliability-summary.md`
- `D:\ChatP2PData\reliability-pack-live\network-status.json`
- `D:\ChatP2PData\reliability-pack-live\failover-smoke.json` as a skipped-step note unless `--include-deterministic-smoke` is used
- `D:\ChatP2PData\reliability-pack-live\primary-inference-proof.json`
- `D:\ChatP2PData\reliability-pack-live\backup-inference-proof.json`

Pass means both lanes are reachable, both expected workers are live, both lanes complete verified echo inference jobs, there are no disputes, and no raw invite token was found in generated artifacts.

The key fields are:

- `summary.can_continue_without_partner`
- `summary.recommended_mode`
- `summary.primary_lane_ready`
- `summary.backup_lane_ready`
- `criteria.token_redaction.passed`

`recommended_mode` can be:

- `primary_and_backup_ready`
- `primary_only`
- `backup_only`
- `blocked`

## Operator Console

When you want the answer without running another proof, generate the static Operator Console:

```powershell
python -m chatp2p.cli operator console `
  --repo D:\Projects\ChatP2P `
  --home D:\ChatP2PData\.mesh `
  --primary-invite D:\ChatP2PData\alpha-invite.json `
  --backup-invite D:\ChatP2PData\backup-alpha-invite-partner.json `
  --expected-primary-worker-id worker_... `
  --expected-backup-worker-id worker_... `
  --reliability-dir D:\ChatP2PData\reliability-pack-live `
  --daily-check-dir D:\ChatP2PData\daily-check `
  --out D:\ChatP2PData\operator-console
```

The console is read-only. It does not create jobs, restart workers, or require partner action. It writes:

- `D:\ChatP2PData\operator-console\operator-console.json`
- `D:\ChatP2PData\operator-console\operator-console.md`
- `D:\ChatP2PData\operator-console\operator-console.html`
- `D:\ChatP2PData\operator-console\operator-console-history.json`
- `D:\ChatP2PData\operator-console\operator-console-cleanup-plan.ps1`

Use `summary.can_continue_without_partner`, `summary.recommended_next_action`, and `action_queue.next_action` as the quick decision fields. The HTML report includes the ranked action queue, scheduled daily-check health, public-repo revision sync, dry-run/execute commands for the next local action, latest self-heal status, and the latest action-run report status so ordinary warnings do not become accidental blockers. The history file records previous console summaries so the report can show what changed since the last run. The cleanup plan lists stale report/proof artifacts for review only; it never deletes files automatically.

Revision sync compares live node-advertised software metadata with the local public repo HEAD by default. Pass `--expected-public-revision <sha>` to pin the comparison to a release commit. Nodes that have not refreshed since revision metadata shipped are reported as `unknown`, not failed; a live node with a different revision produces `wait_for_partner_autopull`, and synced live nodes produce `partner_synced_continue` once the rest of the gate is clear.

When the queue says `wait_for_partner_autopull`, use `operator sync-status` to confirm the state from the latest console snapshot without contacting the partner machine or restarting anything:

```powershell
python -m chatp2p.cli operator sync-status `
  --repo D:\Projects\ChatP2P `
  --console-report D:\ChatP2PData\operator-console\operator-console.json `
  --out D:\ChatP2PData\operator-console\sync-status
```

The report writes `sync-status.json` and `sync-status.md`. Its main state is `synced`, `waiting_for_autopull`, `unknown_old_worker`, or `blocked`, so the operator can tell whether to continue, wait for scheduled autopull, or regenerate Operator Console with network checks.

## Release Check

Before pushing public repo changes, generate the read-only release report:

```powershell
python -m chatp2p.cli operator release-check `
  --repo D:\Projects\ChatP2P `
  --out D:\ChatP2PData\release-check `
  --console-report D:\ChatP2PData\operator-console\operator-console.json `
  --sync-status-report D:\ChatP2PData\maintenance\sync-status\sync-status.json
```

Release check writes `release-check.json` and `release-check.md`. It compares local `HEAD` with local `origin/main`, runs the public privacy scan, reports dirty/ahead/behind state, and recommends `push_origin_main`, `continue_development`, or a blocking fix. It is local and report-only; it does not fetch, push, restart nodes, or contact partner machines.

## Self-Heal

Self-Heal V1 is a report-first local repair planner. It does not execute repairs, restart coordinators, restart workers, or contact partner machines. Generate it from the current console, daily, and action queue reports:

```powershell
python -m chatp2p.cli operator self-heal `
  --console-report D:\ChatP2PData\operator-console\operator-console.json `
  --daily-report D:\ChatP2PData\daily-check\daily-check.json `
  --action-queue D:\ChatP2PData\operator-console\action-queue.json `
  --out D:\ChatP2PData\operator-console
```

It writes:

- `D:\ChatP2PData\operator-console\operator-self-heal-report.json`
- `D:\ChatP2PData\operator-console\operator-self-heal-report.md`

The report covers stale or missing daily-check reports, stale or missing Operator Console reports, stale reliability-pack evidence, missing action-run reports, and public privacy findings. Every V1 self-heal action is local and `partner_required: false`. To run a selected repair later, use the generated `operator run-action --dry-run` command first, then the matching `--execute` command only when you are happy with the exact structured command.

If you want to keep the same offline loop in one step, use:

```powershell
python -m chatp2p.cli operator maintenance `
  --repo C:\Projects\ChatP2P `
  --home C:\ChatP2PData\.mesh `
  --primary-invite C:\Projects\ChatP2P\alpha-invite.json `
  --backup-invite C:\Projects\ChatP2P\backup-alpha-invite.json `
  --out C:\ChatP2PData\maintenance `
  --skip-network-checks `
  --preview-top-action
```

Or the existing helper script:

```powershell
.\scripts\operator-maintenance.ps1 `
  -PrimaryInvite D:\ChatP2PData\alpha-invite.json `
  -BackupInvite D:\ChatP2PData\backup-alpha-invite.json `
  -OutRoot D:\ChatP2PData\maintenance `
  -SkipNetworkChecks `
  -PreviewTopAction
```

This script runs the read-only console, sync-status, daily check, action queue, and self-heal commands in sequence and prints the operator summary. The sync-status step is advisory inside maintenance: if live revision data is unavailable during an offline pass, maintenance records the sync warning without making that advisory check the blocker.

If `operator-maintenance.ps1` is unavailable, the `operator maintenance` command now falls back to a pure-Python sequence of the same subcommands automatically, so the maintenance loop remains usable on systems where PowerShell automation is not present.

To execute the top local action (only when safe and only after reviewing), add `-RunTopAction` + `-AllowExecute`:

```powershell
.\scripts\operator-maintenance.ps1 `
  -PrimaryInvite D:\ChatP2PData\alpha-invite.json `
  -BackupInvite D:\ChatP2PData\backup-alpha-invite.json `
  -OutRoot D:\ChatP2PData\maintenance `
  -SkipNetworkChecks `
  -RunTopAction `
  -AllowExecute
```

## Daily Check

Use daily check when you want one lightweight pass/warn/fail answer:

```powershell
python -m chatp2p.cli operator daily-check `
  --repo D:\Projects\ChatP2P `
  --home D:\ChatP2PData\.mesh `
  --primary-invite D:\ChatP2PData\alpha-invite.json `
  --backup-invite D:\ChatP2PData\backup-alpha-invite-partner.json `
  --reliability-dir D:\ChatP2PData\reliability-pack-live `
  --out D:\ChatP2PData\daily-check `
  --console-out D:\ChatP2PData\operator-console
```

Daily check writes `daily-check.json`, `daily-check.md`, `action-queue.json`, and `action-queue.md`; runs the public privacy scan; updates Operator Console; includes the latest self-heal summary when present; and exits with a clear status. It does not create proof jobs by default. Add `--refresh-reliability-pack` only when you deliberately want to run fresh reliability proof work.

The action queue is the low-burden "what now?" layer. It ranks the next local action, marks whether partner involvement is required, includes token-free suggested PowerShell commands for common local follow-ups, and keeps the operator from treating every warning as a blocker. Regenerate it from an existing daily report with:

```powershell
python -m chatp2p.cli operator action-queue `
  --daily-report D:\ChatP2PData\daily-check\daily-check.json `
  --out D:\ChatP2PData\daily-check
```

Preview the next suggested local action without running it:

```powershell
python -m chatp2p.cli operator run-action `
  --queue D:\ChatP2PData\operator-console\action-queue.json `
  --dry-run
```

`run-action` only executes structured, allowlisted local operator commands when `--execute` is supplied. It refuses free-form shell strings and partner-required actions.

Install the same gate as an hourly local Windows task:

```powershell
Set-Location D:\Projects\ChatP2P
$env:PYTHONPATH='D:\Projects\ChatP2P\src'

python -m chatp2p.cli operator install-daily-check-task `
  --repo D:\Projects\ChatP2P `
  --home D:\ChatP2PData\.mesh `
  --primary-invite D:\ChatP2PData\alpha-invite.json `
  --backup-invite D:\ChatP2PData\backup-alpha-invite-partner.json `
  --reliability-dir D:\ChatP2PData\reliability-pack-live `
  --out D:\ChatP2PData\daily-check `
  --console-out D:\ChatP2PData\operator-console `
  --interval-minutes 60 `
  --work-dir D:\Projects\ChatP2P `
  --allow-startup-folder-fallback
```

Use `--dry-run` first to inspect the generated task plan. `--allow-startup-folder-fallback` installs a per-user Startup folder launcher if Windows denies Scheduled Task creation without elevation.

If you need to pause this lane while you are away, run:

```powershell
python -m chatp2p.cli operator pause `
  --home D:\ChatP2PData\.mesh `
  --daily-task-name "ChatP2P Daily Check" `
  --reliability-task-name "ChatP2P Reliability Pack" `
  --daily-launcher D:\ChatP2PData\.runtime\chatp2p-daily-check.cmd `
  --reliability-launcher D:\ChatP2PData\.runtime\chatp2p-reliability-pack.cmd `
  --keep-launcher
```

This removes both scheduled operator tasks so the workstation stays quiet while your focus is elsewhere.

Resume both automation lanes when you are ready to work again:

```powershell
python -m chatp2p.cli operator resume `
  --repo D:\Projects\ChatP2P `
  --home D:\ChatP2PData\.mesh `
  --primary-invite D:\ChatP2PData\alpha-invite.json `
  --backup-invite D:\ChatP2PData\backup-alpha-invite.json `
  --out-root D:\ChatP2PData `
  --allow-startup-folder-fallback `
  --json
```

Use `--dry-run` to inspect the task plans first. Resume reinstalls Daily Check and Reliability Pack automation only; it does not restart coordinators/workers or contact a partner machine. Follow it with `operator maintenance --skip-network-checks --preview-top-action` for a fresh local decision report.

## Recurring Local Check

Install a local Windows Scheduled Task from the operator machine:

```powershell
Set-Location D:\Projects\ChatP2P
$env:PYTHONPATH='D:\Projects\ChatP2P\src'

python -m chatp2p.cli operator install-reliability-task `
  --primary-invite D:\ChatP2PData\alpha-invite.json `
  --backup-invite D:\ChatP2PData\backup-alpha-invite-partner.json `
  --expected-primary-worker-id worker_PRIMARY `
  --expected-backup-worker-id worker_BACKUP `
  --out D:\ChatP2PData\reliability-pack-live `
  --interval-minutes 30 `
  --work-dir D:\Projects\ChatP2P
```

Use `--dry-run` first when you want to inspect the generated task plan without writing the launcher or creating the task.

The generated launcher lives under:

```text
D:\ChatP2PData\reliability-pack-live\run\
```

Remove the reliability-task automation with:

```powershell
python -m chatp2p.cli operator uninstall-reliability-task `
  --home D:\ChatP2PData\.mesh `
  --task-name "ChatP2P Reliability Pack" `
  --launcher D:\ChatP2PData\.runtime\chatp2p-reliability-pack.cmd
```

Use `--keep-launcher` to leave the generated launcher in place, or `--dry-run` to inspect actions before deletion.

If you prefer direct Scheduled Task cleanup, the legacy command is:

```powershell
schtasks.exe /Delete /TN "ChatP2P Reliability Pack" /F
```

You can also pause both automation lanes at once with:

```powershell
python -m chatp2p.cli operator pause `
  --home D:\ChatP2PData\.mesh `
  --daily-task-name "ChatP2P Daily Check" `
  --reliability-task-name "ChatP2P Reliability Pack"
```

## Notes

The deterministic smoke lane may show jobs as `pending` when only one worker is live, because deterministic verification can require more agreement. The reliability pack therefore skips deterministic smoke by default and uses verified echo inference proof as the stronger "work completed" signal for single-worker lanes.

Run a one-shot pack with deterministic smoke only when you deliberately want that older proof:

```powershell
python -m chatp2p.cli operator reliability-pack `
  --primary-invite D:\ChatP2PData\alpha-invite.json `
  --backup-invite D:\ChatP2PData\backup-alpha-invite-partner.json `
  --expected-primary-worker-id worker_PRIMARY `
  --expected-backup-worker-id worker_BACKUP `
  --out D:\ChatP2PData\reliability-pack-live `
  --include-deterministic-smoke
```

If one lane is down, the pack should still produce a report. The report tells you whether to continue on the remaining lane or treat the system as blocked.
