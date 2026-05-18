# ChatP2P

Peer-contributed AI compute, starting with the boring pieces that have to be true before the dream gets big: signed nodes, signed jobs, verified results, credits, and a coordinator that can later give way to a wider mesh.

## Current Prototype

- A coordinator creates signed job packets.
- A worker node signs its registration.
- The worker verifies leased jobs before running them.
- The worker signs completed results.
- The coordinator verifies results and awards credits.

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

Reputation is computed from signed result history:

- matching results on verified jobs increase score
- mismatches on verified jobs decrease score
- participation in disputed jobs decreases score until better dispute handling exists

Current node reputation states:

- `new`: no terminal verification history yet
- `ok`: positive score with no flags
- `trusted`: score of 3 or higher with no flags
- `watch`: positive score but some mismatch/dispute history
- `flagged`: score below zero

## Product Direction

The first product goal is a one-click node that lets normal machines contribute useful work: deterministic evals, inference jobs, dataset review, verification, model feedback, and later distributed fine-tuning.

The longer blueprint lives in [docs/PRODUCT_BLUEPRINT.md](docs/PRODUCT_BLUEPRINT.md).
