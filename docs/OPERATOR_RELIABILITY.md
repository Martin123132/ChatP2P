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
  --out D:\ChatP2PData\operator-console
```

The console is read-only. It does not create jobs, restart workers, or require partner action. It writes:

- `D:\ChatP2PData\operator-console\operator-console.json`
- `D:\ChatP2PData\operator-console\operator-console.md`
- `D:\ChatP2PData\operator-console\operator-console.html`
- `D:\ChatP2PData\operator-console\operator-console-history.json`
- `D:\ChatP2PData\operator-console\operator-console-cleanup-plan.ps1`

Use `summary.can_continue_without_partner` and `summary.recommended_next_action` as the quick decision fields. The history file records previous console summaries so the report can show what changed since the last run. The cleanup plan lists stale report/proof artifacts for review only; it never deletes files automatically.

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

Remove the task with:

```powershell
schtasks.exe /Delete /TN "ChatP2P Reliability Pack" /F
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
