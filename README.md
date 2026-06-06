# ChatP2P

Peer-contributed AI compute, starting with the boring pieces that have to be true before the dream gets big: signed nodes, signed jobs, verified results, credits, and a coordinator that can later give way to a wider mesh.

## Current Prototype

- A coordinator creates signed job packets.
- A worker node signs its registration.
- The worker verifies leased jobs before running them.
- The worker signs completed results.
- The coordinator verifies results and awards credits.
- Network liveness and job leasing use signed worker packets.

## Quickstart

```bash
python -m pip install -e ".[dev]"
chatp2p quickstart
python -m pytest tests
```

For the smallest product loop on this Windows setup, run one command. It starts a local coordinator and worker, creates one signed echo job, waits for verification, prints the result, and leaves the local loop running so the same command can be repeated:

```powershell
Set-Location D:\Projects\ChatP2P
$env:PYTHONPATH='D:\Projects\ChatP2P\src'

python -m chatp2p.cli quickstart --home D:\ChatP2PData\quickstart
```

Expected shape:

```text
ChatP2P quickstart: pass
Dashboard: http://127.0.0.1:8766/dashboard
Job: job_... (verified)
Worker: worker_...
Result: ChatP2P quickstart: echo this signed job.
Repeat: run the same command again to create another job.
```

That is the sanity bar: start, connect worker, run job, see result, repeat without a second machine or Tailscale.

Before pushing public repo changes, run the privacy gate:

```powershell
python -m chatp2p.cli operator privacy-scan --root D:\Projects\ChatP2P --report D:\ChatP2PData\privacy-scan-report.json
```

The scan fails on committed invite/operator files, real-looking credentials, exact worker IDs, live tailnet IPs, hostnames, and private partner paths in public docs. Matches for credential-shaped values are redacted in the report.

For a fuller pre-push answer, run the read-only release check:

```powershell
python -m chatp2p.cli operator release-check `
  --repo D:\Projects\ChatP2P `
  --out D:\ChatP2PData\release-check `
  --console-report D:\ChatP2PData\operator-console\operator-console.json `
  --sync-status-report D:\ChatP2PData\maintenance\sync-status\sync-status.json
```

Release check writes `release-check.json` and `release-check.md`. It compares local `HEAD` with local `origin/main`, runs the public privacy scan, reports dirty/ahead/behind state, and recommends `push_origin_main`, `continue_development`, or a blocking fix. It does not fetch from GitHub or mutate the repo.

To get a static operator view without starting jobs or another dashboard process, generate the Operator Console report:

```powershell
python -m chatp2p.cli operator console `
  --repo D:\Projects\ChatP2P `
  --home D:\ChatP2PData\.mesh `
  --primary-invite D:\ChatP2PData\alpha-invite.json `
  --backup-invite D:\ChatP2PData\backup-alpha-invite-partner.json `
  --reliability-dir D:\ChatP2PData\reliability-pack-live `
  --daily-check-dir D:\ChatP2PData\daily-check `
  --out D:\ChatP2PData\operator-console
```

The console writes `operator-console.json`, `operator-console.md`, `operator-console.html`, `action-queue.json`, `action-queue.md`, `operator-console-history.json`, and a review-only cleanup plan. It summarizes primary and backup lane health, local managed processes, privacy-scan status, latest reliability-pack evidence, scheduled daily-check health, the ranked action queue, self-heal status, public-repo revision sync, what changed since the previous console run, stale report candidates, and whether the operator can continue without partner action. The HTML also shows the dry-run and execute commands for the next local action, plus the latest `operator-action-run-report.json` and `operator-self-heal-report.json` status when present.

Revision sync compares live node-advertised software metadata with the local public repo HEAD by default. Use `--expected-public-revision <sha>` when you want to pin the comparison to a specific release commit. Nodes that have not refreshed since this feature shipped show `unknown` rather than failing the report.

To turn the latest console snapshot into one focused answer while a partner autopull catches up, run:

```powershell
python -m chatp2p.cli operator sync-status `
  --repo D:\Projects\ChatP2P `
  --console-report D:\ChatP2PData\operator-console\operator-console.json `
  --out D:\ChatP2PData\operator-console\sync-status
```

`sync-status` is read-only and uses the bounded revision metadata already written by Operator Console. It writes `sync-status.json` and `sync-status.md` with one of `synced`, `waiting_for_autopull`, `unknown_old_worker`, or `blocked`, plus the next local recommendation.

To check whether scheduled partner autopull is healthy from existing local evidence, run:

```powershell
python -m chatp2p.cli operator autopull-health `
  --repo D:\Projects\ChatP2P `
  --console-report D:\ChatP2PData\operator-console\operator-console.json `
  --sync-status-report D:\ChatP2PData\operator-console\sync-status\sync-status.json `
  --partner-report D:\ChatP2PData\partner-autopilot-report.json `
  --out D:\ChatP2PData\autopull-health
```

`autopull-health` is read-only and writes `autopull-health.json` and `autopull-health.md`. It classifies the state as `autopull_working`, `autopull_pending`, `autopull_stale`, `partner_offline`, or `unknown`, and keeps `partner_required: false` so it remains a local operator answer rather than a phone-call trigger.

For the daily operator gate, run:

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

Daily check writes `daily-check.json`, `daily-check.md`, `action-queue.json`, and `action-queue.md`; runs the privacy gate; updates the Operator Console; and prints one pass/warn/fail answer. It does not refresh reliability proof jobs unless `--refresh-reliability-pack` is passed. The action queue ranks the next local actions, marks whether partner involvement is required, and includes token-free suggested PowerShell commands for common local follow-ups.

For a read-only repair plan, generate the Operator Self-Heal report:

```powershell
python -m chatp2p.cli operator self-heal `
  --console-report D:\ChatP2PData\operator-console\operator-console.json `
  --daily-report D:\ChatP2PData\daily-check\daily-check.json `
  --action-queue D:\ChatP2PData\operator-console\action-queue.json `
  --out D:\ChatP2PData\operator-console
```

Self-Heal V1 does not run repairs. It lists repairable local report/task issues, selected action ids, and exact dry-run/execute commands for `operator run-action`. V1 covers stale or missing console/daily/action-run reports, stale reliability evidence, and public privacy findings; it does not contact partner machines or restart live nodes.

You can run the same sequence locally with one command:

```powershell
python -m chatp2p.cli operator maintenance `
  --repo C:\Projects\ChatP2P `
  --home C:\ChatP2PData\.mesh `
  --primary-invite C:\ChatP2PData\alpha-invite.json `
  --backup-invite C:\ChatP2PData\backup-alpha-invite.json `
  --out C:\ChatP2PData\maintenance `
  --skip-network-checks `
  --preview-top-action
```

This runs, in order, `operator console`, `operator sync-status`, `operator daily-check`, `operator action-queue`, and `operator self-heal` and prints the summary from the latest console/sync/self-heal pass. The sync-status step is advisory inside maintenance, so missing live revision data is recorded without turning an otherwise useful offline maintenance report into a hard failure. You can add `--preview-top-action` to inspect the generated `run-action` command first.

If `scripts\operator-maintenance.ps1` is missing, the command automatically falls back to an equivalent pure-Python sequence and still writes the same artifact set, including `sync-status\sync-status.json`.

You can also run the same flow with the helper script:

```powershell
.\scripts\operator-maintenance.ps1 `
  -PrimaryInvite D:\ChatP2PData\alpha-invite.json `
  -BackupInvite D:\ChatP2PData\backup-alpha-invite.json `
  -OutRoot D:\ChatP2PData\maintenance `
  -SkipNetworkChecks `
  -PreviewTopAction
```

This runs, in order, `operator console`, `operator sync-status`, `operator daily-check`, `operator action-queue`, and `operator self-heal` and prints the summary from the latest console/sync/self-heal pass. You can add `-PreviewTopAction` to inspect the generated `run-action` command first.

For execute, the script is intentionally explicit:

```powershell
.\scripts\operator-maintenance.ps1 `
  -PrimaryInvite D:\ChatP2PData\alpha-invite.json `
  -BackupInvite D:\ChatP2PData\backup-alpha-invite.json `
  -OutRoot D:\ChatP2PData\maintenance `
  -SkipNetworkChecks `
  -RunTopAction `
  -AllowExecute
```

`-AllowExecute` can also be combined with `-WhatIf` for a final confirmation-style dry-run path.

You can regenerate the queue from an existing daily report:

```powershell
python -m chatp2p.cli operator action-queue `
  --daily-report D:\ChatP2PData\daily-check\daily-check.json `
  --out D:\ChatP2PData\daily-check
```

You can safely preview the next suggested action without running it:

```powershell
python -m chatp2p.cli operator run-action `
  --queue D:\ChatP2PData\operator-console\action-queue.json `
  --dry-run
```

`run-action` executes only structured, allowlisted local operator commands when `--execute` is supplied. It refuses free-form shell strings and partner-required actions.

Install the same check as a local hourly Windows task when you want the workstation to keep producing a fresh operator answer:

```powershell
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

Use `--dry-run` first to inspect the generated launcher and task plan. The scheduled check is read-only by default and does not run proof jobs unless you opt into `--refresh-reliability-pack`.

If you want to stop local daily-check automation on a machine, uninstall the task and launcher with:

```powershell
python -m chatp2p.cli operator uninstall-daily-check-task `
  --home D:\ChatP2PData\.mesh `
  --task-name "ChatP2P Daily Check" `
  --launcher D:\ChatP2PData\.runtime\chatp2p-daily-check.cmd
```

Add `--keep-launcher` if you only want to remove the scheduled task and keep the generated launcher.

If you are going offline for a few days, `operator pause` gives a single command to pause both local reliability lanes:

```powershell
python -m chatp2p.cli operator pause `
  --home D:\ChatP2PData\.mesh `
  --daily-task-name "ChatP2P Daily Check" `
  --reliability-task-name "ChatP2P Reliability Pack" `
  --daily-launcher D:\ChatP2PData\.runtime\chatp2p-daily-check.cmd `
  --reliability-launcher D:\ChatP2PData\.runtime\chatp2p-reliability-pack.cmd `
  --keep-launcher
```

`operator pause` is read-only in the sense that it only uninstalls automation tasks and optional launchers; it does not touch live coordinator/worker processes.

When you are ready to resume local automation, reinstall both lanes with one command:

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

Use `--dry-run` first to inspect both task plans. Resume reinstalls the Daily Check and Reliability Pack tasks only; it does not restart live nodes, create proof jobs, or require partner action. After resume, run `operator maintenance --skip-network-checks --preview-top-action` for the first fresh local answer.

## Network Smoke Test

Terminal 1:

```bash
chatp2p coordinator serve --seed-eval-suite
```

Terminal 2:

```bash
chatp2p worker run-once
```

Benchmark a worker machine before registering it:

```bash
chatp2p node benchmark
```

This saves `.mesh/node-capabilities.json`. Worker commands automatically advertise the saved capability profile, including hardware, CPU score, GPU detection, local model runtime detection, downloaded Ollama models, software revision metadata, and a capability tier such as `light`, `standard`, `gaming_laptop`, or `gpu_worker`.

Check whether this machine is ready to contribute work:

```bash
chatp2p node doctor --model llama3.2:3b
```

The doctor prints JSON checks for worker identity, saved benchmark profile, Ollama reachability, requested model availability, advertised model routing, and coordinator reachability. Use `--skip-coordinator` when you only want local machine checks.

Run a local coordinator and worker in the background:

```bash
chatp2p node up --home D:\ChatP2PData\.mesh --role both
chatp2p node status --home D:\ChatP2PData\.mesh
chatp2p node down --home D:\ChatP2PData\.mesh
```

Managed node state is written under `HOME\run`, and stdout/stderr logs are written under `HOME\logs`. Use `--force` on `node up` to replace an already-managed background process.
When a worker joined from an alpha invite, pass that invite to `node status` so the health check uses the real coordinator URL and redacted admission token instead of the local default:

```bash
chatp2p node status --home E:\ChatP2P-partner\.runtime\.mesh --invite E:\ChatP2P-partner\alpha-invite.json
```

The coordinator stores state in `.mesh/coordinator.sqlite3` by default, so registered nodes, jobs, leases, results, and credits survive restarts.
Leases expire after 30 seconds by default and can be tuned with `--lease-timeout-seconds`.

Credit Ledger V1 records why credits moved, not just each node's current balance. Accepted worker results create `worker_result_reward` ledger entries with the job id, node id, output hash, delta, and balance after the transaction. The legacy `credits` map remains for compatibility, while snapshots include `credit_ledger.summary` and recent entries for auditability. Credits are prototype usage accounting, not money, crypto, or a cash-out promise.

Requester-funded jobs can include `requester_account_id` and `job_cost` when created through `POST /jobs` or `CoordinatorClient.create_job(...)`. The coordinator reserves the job cost before the job enters the queue; if the requester account lacks credits, creation fails and no worker can lease the job. Worker rewards are still recorded separately when an accepted result is submitted.

If a requester-funded job expires with no accepted result, the coordinator records a separate `job_cost_refunded` ledger entry and restores the reserved credits. The original `job_cost_reserved` entry stays in the audit trail. Verified jobs are not refunded.

Chat Inference V1 uses the funded-job path with `inference.chat.v1`. It is Ollama-backed in this phase: workers that advertise the requested local model can lease the job, run the chat messages as a local prompt, return the answer, and earn the configured reward. Create one from the CLI with:

```bash
chatp2p job create-chat --model llama3.2:3b --system "Be concise." --prompt "Explain ChatP2P" --requester-account-id requester_demo --job-cost 1
```

To prove the whole credit-backed chat loop locally without a partner node or model download, run the funded chat smoke. The default `fake` mode starts a temporary local Ollama-compatible endpoint, grants requester credits, reserves the job cost, leases the chat job to a local worker, verifies the signed result, rewards the worker, and writes JSON/Markdown evidence:

```powershell
python -m chatp2p.cli chat smoke `
  --out D:\ChatP2PData\chat-smoke `
  --prompt "Explain ChatP2P" `
  --requester-account-id requester_demo `
  --starting-credits 3 `
  --job-cost 2 `
  --reward 1
```

Use `--mode ollama --model <local-model>` when you deliberately want the smoke to hit a real local Ollama model. The report schema is `chatp2p.funded-chat-smoke-report.v1`.

To ask a running coordinator for a funded chat answer, use `chat ask`. This creates a real `inference.chat.v1` job, reserves credits from the requester account, waits for a worker result, then writes `chat-ask.json` and `chat-ask.md`:

```powershell
python -m chatp2p.cli chat ask `
  --invite D:\ChatP2PData\alpha-invite.json `
  --out D:\ChatP2PData\chat-ask `
  --model llama3.2:3b `
  --prompt "Explain ChatP2P" `
  --requester-account-id requester_demo `
  --job-cost 1
```

The command reads the invite token for job creation but does not print it or write it into the report. Use `--no-wait` when you only want to submit the job and check the result later.

For a repeatable local transcript, use `chat continue`. Each successful run appends one funded user turn to `chat-session.json` and `chat-session.md`, includes recent verified turns as model context, and writes the underlying per-turn `chat-ask` report in a `turn-000N` folder:

```powershell
python -m chatp2p.cli chat continue `
  --invite D:\ChatP2PData\alpha-invite.json `
  --out D:\ChatP2PData\chat-session `
  --session-id demo `
  --model llama3.2:3b `
  --prompt "What did we decide last turn?" `
  --requester-account-id requester_demo `
  --job-cost 1
```

`chat continue` is the safer main loop: it checks the local session first, syncs any existing failed/submitted job-backed turns from the coordinator snapshot, refuses to spend credits if a previous turn is still unresolved, and only then appends the new funded turn. `--max-context-turns` bounds how many verified prior turns are sent as context. The lower-level `chat session` command is still available when you deliberately want to append a turn without the preflight guardrail.

For an interactive local terminal loop, use `chat repl`. Normal messages call the same guarded `chat continue` path, so unresolved failed/submitted turns still block new credit spend until you sync or resume them deliberately:

```powershell
python -m chatp2p.cli chat repl `
  --invite D:\ChatP2PData\alpha-invite.json `
  --out D:\ChatP2PData\chat-session `
  --session-id demo `
  --model llama3.2:3b `
  --requester-account-id requester_demo
```

Inside the REPL, `/status` inspects the local transcript, `/sync` reconciles existing job-backed turns from the coordinator snapshot, `/resume-dry-run` previews retry work without spending credits, and `/quit` exits. The REPL writes `chat-repl.json` and `chat-repl.md` next to the session transcript.

To try the complete chat product loop without a partner node, invite file, or real Ollama install, run the local demo:

```powershell
python -m chatp2p.cli chat demo --port 8787
```

The demo starts a temporary local coordinator, fake Ollama-compatible model endpoint, worker, funded requester account, and chat gateway. Open the printed gateway URL, send a prompt, and the local worker will answer through the same signed funded-job path used by `chat continue`. The demo binds to `127.0.0.1` and stops when you press `Ctrl+C`.

When you have Ollama running locally and a model pulled, switch the same demo to real local inference:

```powershell
python -m chatp2p.cli chat demo --mode ollama --model llama3.2:3b --port 8787
```

`--mode ollama` checks `/api/tags` before opening the gateway. If the model is not advertised, the command fails early with a clear message instead of starting a demo that cannot answer.

For a future UI or local browser test surface, use the localhost-only chat gateway:

```powershell
python -m chatp2p.cli chat gateway `
  --invite D:\ChatP2PData\alpha-invite.json `
  --out D:\ChatP2PData\chat-session `
  --sessions-root D:\ChatP2PData\chat-sessions `
  --session-id demo `
  --model llama3.2:3b `
  --requester-account-id requester_demo `
  --host 127.0.0.1 `
  --port 8787
```

The gateway binds to `127.0.0.1`, serves a small manual chat UI at `/`, and exposes local JSON endpoints for health, conversation list, pre-send readiness, read-only model catalog, session status, privacy-safe transcript, session sync, resume dry-run, session reset dry-run, session archive dry-run, and guarded chat continuation. It reuses the same `chat continue` safety path, so unresolved turns still block new credit spend. The page renders user/assistant turns, a local conversation strip backed by `/api/sessions`, requester balance, coordinator reachability, selected and recommended models, a local model picker backed by `/api/chat/models`, model-capable worker count, turn state, the next safe action, and a copyable local command hint when one is available. `GET /api/chat/readiness?session_id=<session>&model=<model>` and `POST /api/chat/continue` with `{ "prompt": "...", "session_id": "...", "model": "..." }` let a future UI preview and send with a chosen local conversation and advertised model. Conversation ids are constrained to safe local names only. Blocked/fail responses include stable categories such as `coordinator_unreachable`, `insufficient_credits`, `no_model_worker`, `unresolved_session`, `invalid_model`, `invalid_session`, and `request_timeout`, plus safe action hints. The safe-action button only invokes allowlisted no-spend session controls, while reset/archive controls are dry-run reports first. Credit-grant hints remain dry-run by default and require a private operator config.

Inspect a session without creating a job or spending credits:

```powershell
python -m chatp2p.cli chat session-status `
  --out D:\ChatP2PData\chat-session `
  --session-id demo
```

If a turn failed, preview the retry plan first:

```powershell
python -m chatp2p.cli chat session-sync `
  --out D:\ChatP2PData\chat-session `
  --session-id demo `
  --invite D:\ChatP2PData\alpha-invite.json

python -m chatp2p.cli chat session-resume `
  --out D:\ChatP2PData\chat-session `
  --session-id demo `
  --dry-run
```

`session-sync` reads the coordinator snapshot and updates local turns when an already-created job has since verified, expired, or is still active. It does not create jobs or spend credits. Rerun `session-resume` without `--dry-run` only when sync cannot recover the turn. Submitted turns are not retried by default because they may already have reserved credits; use `--include-submitted` only when you deliberately accept possible duplicate spend.

Operator Credit Tools V1 makes requester top-ups explicit and auditable. `bootstrap-alpha` writes an operator-only `credit_grant_token` into the private operator config; this token is not copied into the invite and normal admission tokens cannot grant credits. Inspect balances first:

```powershell
python -m chatp2p.cli operator credits `
  --invite D:\ChatP2PData\alpha-invite.json `
  --requester-account-id requester_demo `
  --out D:\ChatP2PData\operator-credits
```

If the requester needs credits, grant them from the operator machine:

```powershell
python -m chatp2p.cli operator grant-requester-credits `
  --invite D:\ChatP2PData\alpha-invite.json `
  --operator-config D:\ChatP2PData\operator-config.json `
  --requester-account-id requester_demo `
  --credits 10 `
  --out D:\ChatP2PData\operator-credit-grant
```

The grant report records the ledger transaction id and balance after the grant, but it does not print invite tokens or the credit grant token. Use `--dry-run` to write the plan without contacting the coordinator.

Open the coordinator dashboard:

```text
http://127.0.0.1:8765/dashboard
```

Useful local API endpoints:

- `GET /api/snapshot`
- `GET /api/nodes`
- `GET /api/jobs`
- `GET /api/results`
- `GET /api/reputation`
- `GET /api/ledger`

Drain a seeded queue with one worker:

```bash
chatp2p worker loop --max-jobs 4 --stop-when-idle
```

Create jobs after the coordinator is already running:

```bash
chatp2p job create-deterministic --task arithmetic --operation add --operands 7 8
chatp2p job create-deterministic --task number_theory --value 97
chatp2p job create-deterministic --task text --value "open     compute mesh"
chatp2p job create-echo --prompt "hello mesh"
chatp2p job create-ollama --model llama3.2:3b --prompt "Explain peer-to-peer AI in one paragraph"
```

`inference.ollama.v1` jobs are leased only to workers that advertised Ollama support and the requested local model from `chatp2p node benchmark`.
Coordinator snapshots include each job's `resource_requirements` and routing summary, including the required Ollama model and the currently eligible live nodes.
After installing Ollama, pulling a new model, or pulling a new public repo commit, refresh the profile and restart the managed worker so the coordinator sees the new capability and revision:

```bash
chatp2p node refresh-capabilities --home D:\ChatP2PData\.mesh --invite D:\ChatP2PData\alpha-invite.json --restart-worker --report D:\ChatP2PData\capability-refresh-report.json
```
Workers call local Ollama at `http://127.0.0.1:11434` by default; override that with `--ollama-base-url` on `chatp2p worker run-once` or `chatp2p worker loop`.

Inspect jobs and state:

```bash
chatp2p job list
chatp2p job snapshot
chatp2p job reputation
```

## Reliability Proof Harness

Run a one-machine swarm proof with a local coordinator and separate worker processes:

```bash
chatp2p proof swarm --workers 25 --jobs 100 --report .mesh/proof/reliability-report.json
```

The proof writes a full JSON report and prints a short summary with workers registered, jobs verified, accepted results, expired leases, disputes, and pass/fail status.

To prove lease recovery, make a few workers acknowledge a lease and disappear:

```bash
chatp2p proof swarm --workers 25 --jobs 100 --fault-timeout-workers 2
```

Run a local Ollama inference proof after pulling a model:

```bash
ollama pull llama3.2:3b
chatp2p proof ollama --model llama3.2:3b --workers 4 --jobs 8 --report .mesh/proof/ollama-report.json
```

The Ollama proof preflights `/api/tags`, starts a local coordinator, registers separate worker identities that advertise the requested model, creates signed `inference.ollama.v1` jobs, and records result previews in the JSON report. Add `--mismatched-workers 1` to prove workers without the requested model register successfully but do not receive those jobs.
Alpha inference reports also include a routing summary for Ollama mode and fail if a result comes from a node that did not advertise the requested model or if the returned model name does not match.

## Broadband / ISP Edge Simulation

ChatP2P includes a provider-edge proof harness for the broadband-bundle architecture lane. This is not AI inside fibre and not a real ISP deployment; it simulates a provider coordinator, subscriber gateway nodes, provider edge workers, trusted peer workers, policy routing, signed results, verification, and simple usage credits.

```bash
chatp2p operator bootstrap-provider --config D:\ChatP2PData\provider-config.json --provider-name "Demo Fibre AI" --region "Hull"
chatp2p provider create-subscriber --config D:\ChatP2PData\provider-config.json --subscriber-id sub_demo_001 --plan "Broadband AI Plus"
chatp2p proof provider-edge --provider-config D:\ChatP2PData\provider-config.json --subscribers 3 --edge-workers 1 --jobs 25 --report D:\ChatP2PData\provider-edge-proof.json
```

The report schema is `chatp2p.provider-edge-proof-report.v1`. A happy-path pass shows local/provider-edge/peer route counts, verified jobs, zero disputes, zero fallback placeholder routes, and a credit summary. See `docs/PROVIDER_EDGE_MODE.md` for the full runbook.

To wrap the provider proof into a handoff folder and zip, run:

```bash
chatp2p operator provider-ops-pack --provider-config D:\ChatP2PData\provider-config.json --out D:\ChatP2PData\provider-ops-pack --subscribers 3 --edge-workers 1 --jobs 25
```

The ops pack creates `provider-edge-proof.json`, `provider-ops-pack-summary.json`, `provider-ops-pack-summary.md`, `provider-handoff.md`, and `OUT.zip` by default.

To advertise provider roles from a real alpha worker and run provider-shaped work on the live coordinator:

```bash
chatp2p node refresh-capabilities --home D:\ChatP2PData\.mesh --invite D:\ChatP2PData\alpha-invite.json --provider-config D:\ChatP2PData\provider-config.json --node-role provider_edge_worker --restart-worker
chatp2p operator provider-remote-proof --invite D:\ChatP2PData\alpha-invite.json --provider-config D:\ChatP2PData\provider-config.json --expected-worker-id worker_... --jobs 10 --report D:\ChatP2PData\provider-remote-proof.json
chatp2p operator provider-status --invite D:\ChatP2PData\alpha-invite.json --provider-config D:\ChatP2PData\provider-config.json --expected-worker-id worker_... --report D:\ChatP2PData\provider-status.json
```

`provider-status` writes `chatp2p.provider-status-report.v1`, a token-redacted summary of live subscriber/provider-edge/contributor roles, route counts, result counts, credits, expected worker status, and coordinator health. The coordinator dashboard also includes a Provider / ISP Edge panel, and the same totals are available at `/api/provider`.

## Public Alpha Seed Mode

Do not expose a coordinator to the internet without an admission token. Bootstrap an operator config and invite file:

```bash
chatp2p operator bootstrap-alpha --config D:\ChatP2PData\operator-config.json --invite D:\ChatP2PData\alpha-invite.json --coordinator-url http://YOUR_HOST:8765
```

This writes a private operator config and a `chatp2p.alpha-invite.v1` JSON invite. The command generates a strong admission token unless you pass `--admission-token`.

Start the public-alpha coordinator intentionally:

```bash
chatp2p node up --home D:\ChatP2PData\.mesh --role coordinator --host 0.0.0.0 --port 8765 --operator-config D:\ChatP2PData\operator-config.json --force
```

Share only the invite file with accepted contributors. A contributor joins with:

```bash
chatp2p node join --invite alpha-invite.json --home .mesh
```

`node join` creates or reuses a worker identity, runs `node benchmark` if no capability profile exists, checks coordinator health, starts a managed background worker, and waits for it to appear live. Logs are written under `HOME\logs`.

Troubleshooting:

- token rejected: ask the operator for a fresh invite file
- coordinator unreachable: confirm the `coordinator` URL in the invite is reachable from your machine
- missing Ollama: deterministic and echo jobs still work, but Ollama jobs need Ollama running locally
- stale capabilities or revision metadata: run `chatp2p node refresh-capabilities --home .mesh --invite alpha-invite.json --restart-worker` after installing Ollama, pulling models, or pulling repo updates

Workers and job producers can still pass the token manually when needed:

```bash
chatp2p worker loop --coordinator http://YOUR_HOST:8765 --admission-token "change-this-long-token"
chatp2p job create-echo --coordinator http://YOUR_HOST:8765 --admission-token "change-this-long-token" --prompt "hello mesh"
```

Public alpha mode requires the token for node registration and job creation, limits request body size, limits public job payload size, and restricts job types to the operator allow-list. It does not open firewall ports, configure routers, or create public tunnels.

Before inviting contributors, run:

```bash
chatp2p operator alpha-preflight --config D:\ChatP2PData\operator-config.json --invite D:\ChatP2PData\alpha-invite.json --home D:\ChatP2PData\.mesh --report D:\ChatP2PData\alpha-preflight-report.json
```

For remote contributors, the invite must use a URL they can reach. `localhost`, `127.0.0.1`, and private LAN addresses such as `192.168.x.x` are only valid on the same machine, same LAN, or an intentional VPN/tunnel.

Check the current invite route before sending it:

```bash
chatp2p operator alpha-route --home D:\ChatP2PData\.mesh --report D:\ChatP2PData\alpha-route-report.json
```

For the first remote partner test, put both machines on the same Tailscale tailnet, regenerate the invite with this machine's Tailscale IP, restart the managed node, then rerun `alpha-route`. A ready tailnet invite reports `current_route.reachability.kind == "tailnet_self"` and `status == "pass"`.

After at least one contributor joins, prove the alpha can complete signed work:

```bash
chatp2p operator alpha-smoke --invite D:\ChatP2PData\alpha-invite.json --jobs 4 --min-live-workers 1 --min-accepted-results 1 --report D:\ChatP2PData\alpha-smoke-report.json
```

For a two-machine partner proof, use the worker ID printed by `node join` and require every proof-created job to verify:

```bash
chatp2p operator alpha-remote-proof --invite D:\ChatP2PData\alpha-invite.json --expected-worker-id worker_... --jobs 4 --report D:\ChatP2PData\alpha-remote-proof-report.json
```

Pass means the expected worker was live, it returned at least one accepted result, every job created by the proof reached a terminal state, all proof-created jobs verified, and no proof-created jobs disputed or expired.

For an inference-style proof, start with echo mode:

```bash
chatp2p operator alpha-inference-proof --invite D:\ChatP2PData\alpha-invite.json --expected-worker-id worker_... --jobs 10 --min-live-workers 2 --report D:\ChatP2PData\alpha-inference-proof-report.json
```

Echo mode creates signed `inference.echo.v1` jobs and verifies the result path that real model inference will use. When workers advertise a local Ollama model, use `--mode ollama --model MODEL`, or `--mode auto --model MODEL` to use Ollama only when a live capable node is present.

For a longer two-machine stability run, use the soak harness. It runs repeated inference-proof rounds, writes one sidecar report per round, and rolls everything into a single pass/fail report:

```bash
chatp2p operator alpha-soak --invite D:\ChatP2PData\alpha-invite.json --expected-worker-id worker_... --min-live-workers 2 --jobs-per-round 10 --rounds 6 --round-interval-seconds 30 --min-expected-worker-results-total 10 --report D:\ChatP2PData\alpha-soak-report.json
```

Pass means every completed round met its live-worker, accepted-result, verified-job, and zero-dispute/zero-expiry thresholds, and the expected worker met the configured total contribution threshold. Add `--duration-seconds 3600` when you want a wall-clock cap for a longer unattended run. Use `--min-expected-worker-results-per-round` only when you intentionally need that worker to contribute in every round.

For a quick health check, use:

```bash
chatp2p operator alpha-status --home D:\ChatP2PData\.mesh --invite D:\ChatP2PData\alpha-invite.json --expected-worker-id worker_... --min-live-workers 2 --report D:\ChatP2PData\alpha-status-report.json
```

To collect a shareable redacted evidence folder after a partner joins, run:

```bash
chatp2p operator alpha-evidence --home D:\ChatP2PData\.mesh --invite D:\ChatP2PData\alpha-invite.json --expected-worker-id worker_... --jobs 25 --out D:\ChatP2PData\alpha-evidence --include-inference-proof --inference-mode echo --inference-jobs 20
```

The evidence pack writes `alpha-status.json`, `alpha-remote-proof.json`, optional `alpha-evidence-inference-proof.json`, a copied watchdog report, a Windows task query report, and `alpha-evidence-summary.md`. The status and inference reports show each live node's supported job types and advertised Ollama models when present. The raw admission token is redacted before files are written.

To wrap that into an operator handoff folder and zip, run:

```bash
chatp2p operator alpha-ops-pack --home D:\ChatP2PData\.mesh --invite D:\ChatP2PData\alpha-invite.json --out D:\ChatP2PData\alpha-ops-pack-live --expected-worker-id worker_... --include-routing-evidence
```

The ops pack creates a nested evidence folder, `alpha-ops-pack-summary.json`, `alpha-ops-pack-summary.md`, `operator-handoff.md`, `partner-handoff.md`, and `OUT.zip` by default. It is meant to be the repeatable "show the measurements" artifact for an alpha operator. The original invite, operator config, runtime homes, identities, and SQLite databases remain private.

For a two-lane operator check that does not require the partner to run anything manually, use the reliability pack:

```powershell
chatp2p operator reliability-pack `
  --primary-invite D:\ChatP2PData\alpha-invite.json `
  --backup-invite D:\ChatP2PData\backup-alpha-invite-partner.json `
  --expected-primary-worker-id worker_... `
  --expected-backup-worker-id worker_... `
  --out D:\ChatP2PData\reliability-pack-live
```

The reliability pack writes network status, primary/backup verified echo inference proofs, token-redaction checks, and a `reliability-summary.md`. Deterministic failover smoke is skipped by default to avoid leaving single-worker proof jobs pending; use `--include-deterministic-smoke` when you deliberately want that older proof. Install a local recurring Windows check with `chatp2p operator install-reliability-task ... --interval-minutes 30`. Full details live in [docs/OPERATOR_RELIABILITY.md](docs/OPERATOR_RELIABILITY.md).

If you need to remove a scheduled reliability pack task, use:

```powershell
python -m chatp2p.cli operator uninstall-reliability-task `
  --home D:\ChatP2PData\.runtime `
  --task-name "ChatP2P Reliability Pack" `
  --launcher D:\ChatP2PData\.runtime\chatp2p-reliability-pack.cmd
```

For a read-only snapshot of what to do next, run `chatp2p operator console ...`. It does not create proof jobs, restart processes, or delete old artifacts; it turns existing health, reliability, privacy, autopilot, history, and stale-report evidence into one static operator page.

To let the node check and restart managed processes from the invite, run a one-shot watchdog check:

```bash
chatp2p node watchdog --home D:\ChatP2PData\.mesh --invite D:\ChatP2PData\alpha-invite.json --operator-config D:\ChatP2PData\operator-config.json --role both --report D:\ChatP2PData\node-watchdog-report.json
```

Use `--checks 0` to keep the watchdog running until interrupted. Reports redact the invite token and managed process command secrets.

On Windows, install that watchdog as a Scheduled Task:

```bash
chatp2p node install-task --home D:\ChatP2PData\.mesh --invite D:\ChatP2PData\alpha-invite.json --operator-config D:\ChatP2PData\operator-config.json --role both --task-name "ChatP2P Operator Watchdog" --report D:\ChatP2PData\node-watchdog-report.json
```

Managed coordinator startup can take longer as the SQLite evidence database grows, so watchdog and task restarts now wait `90` seconds by default. Keep runtime files and generated launchers on the D drive unless you intentionally use `--allow-startup-folder-fallback`.

Contributor machines can install a worker-only task without the operator config:

```bash
chatp2p node install-task --home E:\ChatP2P-partner\.runtime\.mesh --invite E:\ChatP2P-partner\alpha-invite.json --role worker --task-name "ChatP2P Worker Watchdog" --report E:\ChatP2P-partner\.runtime\node-watchdog-report.json
```

After a worker-only node is running, check it with the invite-backed status command:

```bash
chatp2p node status --home E:\ChatP2P-partner\.runtime\.mesh --invite E:\ChatP2P-partner\alpha-invite.json
```

Remove a task with `chatp2p node uninstall-task --task-name "ChatP2P Worker Watchdog" --home E:\ChatP2P-partner\.runtime\.mesh`.

If Windows denies Scheduled Task creation from a non-elevated terminal, rerun from an elevated terminal. A per-user Startup folder fallback is available with `--allow-startup-folder-fallback`, but that writes a small launcher under `%APPDATA%`, so avoid it when you want every ChatP2P file kept on the runtime drive.

When you are waiting for a real contributor, rehearse the same flow with an isolated local simulated worker:

```bash
chatp2p operator alpha-drill --home D:\ChatP2PData\.mesh --simulated-workers 1 --jobs 4 --report D:\ChatP2PData\alpha-drill-report.json
```

The full alpha runbook lives in [docs/ALPHA_RUNBOOK.md](docs/ALPHA_RUNBOOK.md).

Generic JSON job creation is also available:

```bash
chatp2p job create --job-type inference.echo.v1 --payload-json "{\"prompt\":\"hello mesh\"}"
```

## Verification Status

Jobs no longer become trusted just because one worker replied.

- `signature-and-schema-check` jobs need one signed result.
- `duplicate-on-random-sample` jobs need two independent workers with matching output hashes.
- If duplicate results disagree, the coordinator leases the job to a third worker as a tie-breaker.
- If no output reaches quorum after the tie-breaker, the job becomes `disputed`.

Current job states:

- `queued`: waiting for a worker
- `leased`: sent to a worker, no result yet
- `pending`: at least one result exists, but quorum is not reached
- `verified`: enough matching results exist
- `disputed`: max verification attempts used without quorum
- `expired`: job deadline passed before terminal verification

## Reputation

Reputation is computed from signed result and lease history:

- matching results on verified jobs increase score
- mismatches on verified jobs decrease score
- participation in disputed jobs decreases score until better dispute handling exists
- expired leases add a small timeout penalty

Current node reputation states:

- `new`: no terminal verification history yet
- `ok`: positive score with no flags
- `trusted`: score of 3 or higher with no flags
- `watch`: some timeout history or non-terminal concern
- `flagged`: score below zero

## Lease Recovery and Liveness

Workers are expected to disappear sometimes, so the coordinator now treats leases as temporary:

- every lease has `leased_at` and `expires_at`
- job pulls use signed `job.lease-request.v1` packets
- the coordinator replies with a signed `job.lease-grant.v1`
- workers sign a `job.lease-ack.v1` acknowledgement over the grant hash
- long-running jobs send signed `job.lease-renewal.v1` packets before the active lease expires
- signed heartbeat, lease request, lease acknowledgement, and lease renewal packets are rejected when stale or replayed
- expired leases are removed from the active queue and can be picked up by another worker
- late results from expired leases are rejected
- node `last_seen_at` is updated on registration, heartbeat, job pull, and result submission
- node liveness is reported as `live`, `stale`, or `offline`

Lease and liveness state is exposed in the dashboard, `GET /api/snapshot`, and `GET /health`. Workers can ping `POST /nodes/heartbeat` with a signed `node.heartbeat.v1` packet. The legacy unsigned `GET /jobs/next?node_id=...` path is rejected.

## Trust-Aware Leasing

Workers pull jobs from the coordinator, so reputation affects which job the coordinator offers next:

- `trusted` and `ok` workers get pending verification and tie-breaker work before ordinary queued work.
- `new` workers get ordinary queued work first, then pending verification work if no queue work is available.
- `watch` workers can still work, but they are not preferred for normal quorum verification.
- `flagged` workers do not receive ordinary queued work.
- `flagged` workers may receive a conflicting pending job only when the coordinator needs a tie-breaker result.

The active leasing policy is included in `GET /api/snapshot` and `GET /health`.

## Product Direction

The first product goal is a one-click node that lets normal machines contribute useful work: deterministic evals, inference jobs, dataset review, verification, model feedback, and later distributed fine-tuning.

Model Governance V0 turns the "community-shaped model" idea into a local registry contract. It defines membership tiers, approved weight-pack rules, adapter submission gates, domain review roles, and tamper response. It does not train or download a model; it records the rules for who may influence future model/adaptor releases and rejects direct core-weight editing in V0.

```powershell
python -m chatp2p.cli model governance `
  --registry D:\ChatP2PData\model-governance.json `
  --out D:\ChatP2PData\model-governance-report.json `
  --init `
  --json
```

The default registry intentionally starts with a placeholder base weight pack, so the report warns until a real open-weight base model, license, and hashes are selected. Credits remain spendable usage accounting; reputation and tier gates decide who can submit adapters, review domains, or vote on releases.

The longer blueprint lives in [docs/PRODUCT_BLUEPRINT.md](docs/PRODUCT_BLUEPRINT.md).
