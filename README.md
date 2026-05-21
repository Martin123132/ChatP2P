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
chatp2p demo
python -m pytest tests
```

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

This saves `.mesh/node-capabilities.json`. Worker commands automatically advertise the saved capability profile, including hardware, CPU score, GPU detection, local model runtime detection, downloaded Ollama models, and a capability tier such as `light`, `standard`, `gaming_laptop`, or `gpu_worker`.

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
chatp2p node status --home E:\ChatP2P-private-version--main\.runtime\.mesh --invite E:\ChatP2P-private-version--main\alpha-invite.json
```

The coordinator stores state in `.mesh/coordinator.sqlite3` by default, so registered nodes, jobs, leases, results, and credits survive restarts.
Leases expire after 30 seconds by default and can be tuned with `--lease-timeout-seconds`.

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
After installing Ollama or pulling a new model, refresh the profile and restart the managed worker so the coordinator sees the new capability:

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
- stale capabilities: run `chatp2p node refresh-capabilities --home .mesh --invite alpha-invite.json --restart-worker` after installing Ollama or pulling models

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

To let the node check and restart managed processes from the invite, run a one-shot watchdog check:

```bash
chatp2p node watchdog --home D:\ChatP2PData\.mesh --invite D:\ChatP2PData\alpha-invite.json --operator-config D:\ChatP2PData\operator-config.json --role both --report D:\ChatP2PData\node-watchdog-report.json
```

Use `--checks 0` to keep the watchdog running until interrupted. Reports redact the invite token and managed process command secrets.

On Windows, install that watchdog as a Scheduled Task:

```bash
chatp2p node install-task --home D:\ChatP2PData\.mesh --invite D:\ChatP2PData\alpha-invite.json --operator-config D:\ChatP2PData\operator-config.json --role both --task-name "ChatP2P Operator Watchdog" --report D:\ChatP2PData\node-watchdog-report.json
```

Contributor machines can install a worker-only task without the operator config:

```bash
chatp2p node install-task --home E:\ChatP2P-private-version--main\.runtime\.mesh --invite E:\ChatP2P-private-version--main\alpha-invite.json --role worker --task-name "ChatP2P Worker Watchdog" --report E:\ChatP2P-private-version--main\.runtime\node-watchdog-report.json
```

After a worker-only node is running, check it with the invite-backed status command:

```bash
chatp2p node status --home E:\ChatP2P-private-version--main\.runtime\.mesh --invite E:\ChatP2P-private-version--main\alpha-invite.json
```

Remove a task with `chatp2p node uninstall-task --task-name "ChatP2P Worker Watchdog" --home E:\ChatP2P-private-version--main\.runtime\.mesh`.

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

The longer blueprint lives in [docs/PRODUCT_BLUEPRINT.md](docs/PRODUCT_BLUEPRINT.md).
