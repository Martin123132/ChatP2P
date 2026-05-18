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

## Product Direction

The first product goal is a one-click node that lets normal machines contribute useful work: deterministic evals, inference jobs, dataset review, verification, model feedback, and later distributed fine-tuning.

The longer blueprint lives in [docs/PRODUCT_BLUEPRINT.md](docs/PRODUCT_BLUEPRINT.md).
