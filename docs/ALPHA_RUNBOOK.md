# ChatP2P Alpha Runbook

This runbook is for a small closed alpha where one operator starts a token-gated coordinator and accepted contributors join as managed workers.

## Operator Setup

Keep repo work on `D:\Projects\ChatP2P` and runtime data on `D:\ChatP2PData`.

```bash
cd D:\Projects\ChatP2P
set PYTHONPATH=D:\Projects\ChatP2P\src
python -m chatp2p.cli operator bootstrap-alpha --config D:\ChatP2PData\operator-config.json --invite D:\ChatP2PData\alpha-invite.json --coordinator-url http://YOUR_HOST:8765 --force
python -m chatp2p.cli node up --home D:\ChatP2PData\.mesh --role coordinator --host 0.0.0.0 --port 8765 --operator-config D:\ChatP2PData\operator-config.json --force
```

Do not share `operator-config.json`. Share only `alpha-invite.json` with accepted contributors. The invite still contains the admission token, so treat it as private.

## Preflight

Run preflight before inviting contributors:

```bash
python -m chatp2p.cli operator alpha-preflight --config D:\ChatP2PData\operator-config.json --invite D:\ChatP2PData\alpha-invite.json --home D:\ChatP2PData\.mesh --report D:\ChatP2PData\alpha-preflight-report.json
```

Pass means the config and invite load, the invite token matches the operator config, the coordinator is reachable, public-alpha mode is active, and the health summary does not expose the raw token.

Warnings are worth reading. A localhost or private LAN invite URL, such as `127.0.0.1` or `192.168.x.x`, means outside contributors cannot use the invite until the coordinator URL points at a reachable VPN, tunnel, or public host.

## Remote Partner Connectivity

For a trusted partner outside your home network, the invite `coordinator` URL must be reachable from their machine. A private LAN address like `http://192.168.4.90:8765` is only useful on the same LAN, or across a VPN/tailnet that makes that address reachable.

Safe first options:

- Private VPN/tailnet: put both machines on the same private network, then regenerate the invite with the VPN/tailnet address.
- HTTPS tunnel: map a public hostname to the local coordinator and regenerate the invite with that hostname.
- Router port forward: only use this deliberately; keep public alpha token-gated and avoid exposing unauthenticated coordinators.

After changing the reachable URL, regenerate the invite without changing the runtime home:

```bash
python -m chatp2p.cli operator bootstrap-alpha --config D:\ChatP2PData\operator-config.json --invite D:\ChatP2PData\alpha-invite.json --coordinator-url http://REACHABLE_HOST:8765 --force
python -m chatp2p.cli operator alpha-preflight --config D:\ChatP2PData\operator-config.json --invite D:\ChatP2PData\alpha-invite.json --home D:\ChatP2PData\.mesh --report D:\ChatP2PData\alpha-preflight-report.json
```

The coordinator must be restarted after rotating or regenerating the operator config because it reads the admission token at startup.

## Operator Drill

When no outside contributor is available yet, run a local rehearsal with an isolated simulated worker:

```bash
python -m chatp2p.cli operator alpha-drill --home D:\ChatP2PData\.mesh --simulated-workers 1 --jobs 4 --report D:\ChatP2PData\alpha-drill-report.json
```

The drill checks or starts the coordinator, starts the primary worker if needed, starts simulated workers under `D:\ChatP2PData\.mesh-alpha-drill`, runs preflight, then runs a quorum smoke proof. Pass means the drill observed enough live workers, accepted results, verified jobs, and zero disputes. It writes sidecar reports next to the main report:

- `D:\ChatP2PData\alpha-drill-report-preflight.json`
- `D:\ChatP2PData\alpha-drill-report-smoke.json`

Use `--cleanup-simulated-workers` when you want the simulated workers stopped after the report is written.

## Contributor Join

Contributor quickstart:

```bash
python -m pip install -e ".[dev]"
chatp2p node join --invite alpha-invite.json --home .mesh
```

The join command creates or reuses a worker identity, benchmarks the machine if needed, starts a managed worker, and waits until the coordinator sees it as live.

## Smoke Proof

Once at least one contributor has joined, run:

```bash
python -m chatp2p.cli operator alpha-smoke --invite D:\ChatP2PData\alpha-invite.json --jobs 4 --min-live-workers 1 --min-accepted-results 1 --min-verified-jobs 0 --timeout-seconds 90 --report D:\ChatP2PData\alpha-smoke-report.json
```

Pass means at least one live worker was observed, at least one smoke-created deterministic job got an accepted signed result, and none of the smoke-created jobs became disputed. `min-verified-jobs` defaults to `0` because a one-worker alpha can produce accepted results while duplicate verification remains pending.

## Rollback

Stop the local operator node:

```bash
python -m chatp2p.cli node down --home D:\ChatP2PData\.mesh --role both
```

Rotate the alpha token by generating a new config and invite:

```bash
python -m chatp2p.cli operator bootstrap-alpha --config D:\ChatP2PData\operator-config.json --invite D:\ChatP2PData\alpha-invite.json --coordinator-url http://YOUR_HOST:8765 --force
```

Old invites stop working after the coordinator restarts with the new operator config.
