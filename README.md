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
```

Inspect jobs and state:

```bash
chatp2p job list
chatp2p job snapshot
chatp2p job reputation
```

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
