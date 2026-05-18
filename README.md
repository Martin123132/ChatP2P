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
```

Generic JSON job creation is also available:

```bash
chatp2p job create --job-type inference.echo.v1 --payload-json "{\"prompt\":\"hello mesh\"}"
```

## Product Direction

The first product goal is a one-click node that lets normal machines contribute useful work: deterministic evals, inference jobs, dataset review, verification, model feedback, and later distributed fine-tuning.

The longer blueprint lives in [docs/PRODUCT_BLUEPRINT.md](docs/PRODUCT_BLUEPRINT.md).
