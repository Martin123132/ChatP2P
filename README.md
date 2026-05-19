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

## Public Alpha Seed Mode

Do not expose a coordinator to the internet without an admission token. Write an operator config:

```bash
chatp2p operator write-config --output D:\ChatP2PData\operator-config.json --admission-token "change-this-long-token"
```

Start a coordinator with the config:

```bash
chatp2p coordinator serve --host 0.0.0.0 --port 8765 --home D:\ChatP2PData\.mesh --operator-config D:\ChatP2PData\operator-config.json
```

Workers and job producers pass the same token:

```bash
chatp2p worker loop --coordinator http://YOUR_HOST:8765 --admission-token "change-this-long-token"
chatp2p job create-echo --coordinator http://YOUR_HOST:8765 --admission-token "change-this-long-token" --prompt "hello mesh"
```

Public alpha mode requires the token for node registration and job creation, limits request body size, limits public job payload size, and restricts job types to the operator allow-list.

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
- signed heartbeat, lease request, and lease acknowledgement packets are rejected when stale or replayed
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
