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

Run a route report before sending the invite:

```bash
python -m chatp2p.cli operator alpha-route --home D:\ChatP2PData\.mesh --report D:\ChatP2PData\alpha-route-report.json
```

The route report classifies the current invite URL, checks coordinator health, inspects managed process state, and detects local route tools such as Tailscale or cloudflared. On Windows it also checks the standard Tailscale install path, because a fresh install may not appear on `PATH` until a new terminal is opened. It does not open firewall ports, configure routers, log in to VPNs, or create tunnels.

Safe first options:

- Private VPN/tailnet: put both machines on the same private network, then regenerate the invite with the VPN/tailnet address.
- HTTPS tunnel: map a public hostname to the local coordinator and regenerate the invite with that hostname.
- Router port forward: only use this deliberately; keep public alpha token-gated and avoid exposing unauthenticated coordinators.

Tailscale first-test flow:

```bash
tailscale ip -4
python -m chatp2p.cli operator alpha-route --home D:\ChatP2PData\.mesh --candidate-url http://TAILSCALE_IP:8765 --report D:\ChatP2PData\alpha-route-report.json
python -m chatp2p.cli operator bootstrap-alpha --config D:\ChatP2PData\operator-config.json --invite D:\ChatP2PData\alpha-invite.json --coordinator-url http://TAILSCALE_IP:8765 --force
python -m chatp2p.cli node up --home D:\ChatP2PData\.mesh --role both --host 0.0.0.0 --port 8765 --coordinator http://TAILSCALE_IP:8765 --operator-config D:\ChatP2PData\operator-config.json --worker-interval 0.5 --force
python -m chatp2p.cli operator alpha-route --home D:\ChatP2PData\.mesh --report D:\ChatP2PData\alpha-route-report.json
```

`alpha-route` should report `current_route.reachability.kind == "tailnet_self"` and `status == "pass"` once the invite points at this machine's Tailscale IP and the coordinator is healthy. `alpha-preflight` may still warn that `100.64.0.0/10` is shared address space; that warning is expected for generic invite validation and means the partner must be on the same tailnet.

After changing the reachable URL, regenerate the invite without changing the runtime home:

```bash
python -m chatp2p.cli operator bootstrap-alpha --config D:\ChatP2PData\operator-config.json --invite D:\ChatP2PData\alpha-invite.json --coordinator-url http://REACHABLE_HOST:8765 --force
python -m chatp2p.cli operator alpha-preflight --config D:\ChatP2PData\operator-config.json --invite D:\ChatP2PData\alpha-invite.json --home D:\ChatP2PData\.mesh --report D:\ChatP2PData\alpha-preflight-report.json
```

To test a possible URL before rewriting the invite, pass it as a candidate:

```bash
python -m chatp2p.cli operator alpha-route --home D:\ChatP2PData\.mesh --candidate-url http://REACHABLE_HOST:8765 --report D:\ChatP2PData\alpha-route-report.json
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

## Remote Partner Proof

When a real partner has joined, copy the `worker_node_id` from their `node join` output or from your coordinator dashboard, then run:

```bash
python -m chatp2p.cli operator alpha-remote-proof --invite D:\ChatP2PData\alpha-invite.json --expected-worker-id worker_87b5cefe53e67c6c --jobs 4 --timeout-seconds 180 --report D:\ChatP2PData\alpha-remote-proof-report.json
```

Pass means:

- at least two workers were live
- the expected partner worker was live
- the expected partner worker returned at least one accepted result
- every job created by this proof reached a terminal state
- every job created by this proof verified
- no proof-created job disputed or expired

The report schema is `chatp2p.alpha-remote-proof-report.v1`. It includes pre-existing coordinator counts separately from the proof-created jobs, so old dashboard history does not hide whether this run passed.

## Inference Proof

After deterministic remote proof is passing, run an inference-style packet proof:

```bash
python -m chatp2p.cli operator alpha-inference-proof --invite D:\ChatP2PData\alpha-invite.json --expected-worker-id worker_87b5cefe53e67c6c --jobs 10 --min-live-workers 2 --report D:\ChatP2PData\alpha-inference-proof-report.json
```

The default mode is `echo`, which creates `inference.echo.v1` jobs. These jobs do not call a model, but they use the inference job schema, signed lease/result flow, worker dispatch path, and one-result verification strategy that model jobs use.

When a live worker advertises a local Ollama model, run:

```bash
python -m chatp2p.cli operator alpha-inference-proof --invite D:\ChatP2PData\alpha-invite.json --mode ollama --model llama3.2:3b --jobs 2 --min-live-workers 1 --report D:\ChatP2PData\alpha-ollama-proof-report.json
```

Use `--mode auto --model MODEL` when you want the command to use Ollama if a live capable node advertises the model, and otherwise fall back to echo.
Workers renew active leases while blocked inside local model inference, so an Ollama proof can run past the default 30-second lease timeout without losing the job.

If a node installs Ollama or pulls a new model after it already joined, refresh its capability profile and restart the managed worker:

```bash
python -m chatp2p.cli node refresh-capabilities --home D:\ChatP2PData\.mesh --invite D:\ChatP2PData\alpha-invite.json --restart-worker --report D:\ChatP2PData\capability-refresh-report.json
```

Contributor machines use the same command with their own runtime home and private invite path. The refresh report shows newly advertised job types, Ollama models, and whether the restarted worker registered successfully.

## Alpha Status

Use this when you want a single redacted health report without creating new jobs:

```bash
python -m chatp2p.cli operator alpha-status --home D:\ChatP2PData\.mesh --invite D:\ChatP2PData\alpha-invite.json --expected-worker-id worker_87b5cefe53e67c6c --min-live-workers 2 --report D:\ChatP2PData\alpha-status-report.json
```

Pass means the coordinator health endpoint is reachable, the local managed processes are healthy enough for the configured home, the minimum live worker count is present, the expected worker is live when provided, and there are no disputed jobs. A backlog of queued, pending, or leased jobs is reported as a warning rather than a failure.

## Alpha Evidence Pack

After a real partner has joined and the remote proof passes, collect a redacted evidence folder:

```bash
python -m chatp2p.cli operator alpha-evidence --home D:\ChatP2PData\.mesh --invite D:\ChatP2PData\alpha-invite.json --expected-worker-id worker_87b5cefe53e67c6c --jobs 25 --out D:\ChatP2PData\alpha-evidence --include-inference-proof --inference-mode echo --inference-jobs 20
```

The command writes:

- `D:\ChatP2PData\alpha-evidence\alpha-status.json`
- `D:\ChatP2PData\alpha-evidence\alpha-remote-proof.json`
- `D:\ChatP2PData\alpha-evidence\alpha-evidence-inference-proof.json` when `--include-inference-proof` is used
- `D:\ChatP2PData\alpha-evidence\node-watchdog-report.json`
- `D:\ChatP2PData\alpha-evidence\operator-watchdog-task.json`
- `D:\ChatP2PData\alpha-evidence\alpha-evidence-summary.json`
- `D:\ChatP2PData\alpha-evidence\alpha-evidence-summary.md`

Pass means the current alpha status passed, the command created fresh deterministic proof jobs, the optional inference proof passed, the expected partner worker contributed accepted results, all proof-created jobs verified, and no raw admission token was found in the evidence artifacts. Missing watchdog or Scheduled Task evidence is a warning so the network proof remains usable while you are still setting up background reliability.

## Node Watchdog

Run a one-shot check and restart unhealthy managed roles:

```bash
python -m chatp2p.cli node watchdog --home D:\ChatP2PData\.mesh --invite D:\ChatP2PData\alpha-invite.json --operator-config D:\ChatP2PData\operator-config.json --role both --report D:\ChatP2PData\node-watchdog-report.json
```

The watchdog uses the invite file to restart workers, so it does not need to recover the admission token from redacted process state. The operator config is only required when the watchdog may restart the coordinator. Use `--role worker` on a contributor machine, and omit `--operator-config` there:

```bash
python -m chatp2p.cli node watchdog --home E:\ChatP2P-private-version--main\.runtime\.mesh --invite E:\ChatP2P-private-version--main\alpha-invite.json --role worker --report E:\ChatP2P-private-version--main\.runtime\node-watchdog-report.json
```

Use `--checks 0` to keep the watchdog running until interrupted, or leave the default `--checks 1` for a safe one-shot health repair.

## Windows Scheduled Task

Install the operator watchdog so Windows starts it again after login:

```bash
python -m chatp2p.cli node install-task --home D:\ChatP2PData\.mesh --invite D:\ChatP2PData\alpha-invite.json --operator-config D:\ChatP2PData\operator-config.json --role both --task-name "ChatP2P Operator Watchdog" --report D:\ChatP2PData\node-watchdog-report.json
```

The installer writes a generated `.cmd` launcher under `D:\ChatP2PData\.mesh\run` and creates a Windows Scheduled Task that runs the watchdog with `--checks 0`. The task command references the invite path, not the raw admission token.

If Windows returns `Access is denied` while creating the Scheduled Task, rerun the command from an elevated terminal. A no-admin Startup folder fallback is available with `--allow-startup-folder-fallback`, but it writes a small `.vbs` launcher under `%APPDATA%`; avoid that fallback when you want every ChatP2P file kept on the runtime drive.

On a contributor machine, install only the worker watchdog:

```bash
python -m chatp2p.cli node install-task --home E:\ChatP2P-private-version--main\.runtime\.mesh --invite E:\ChatP2P-private-version--main\alpha-invite.json --role worker --task-name "ChatP2P Worker Watchdog" --report E:\ChatP2P-private-version--main\.runtime\node-watchdog-report.json
```

Use `--dry-run` first if you want to inspect the exact task plan without creating it. Remove tasks with:

```bash
python -m chatp2p.cli node uninstall-task --task-name "ChatP2P Operator Watchdog" --home D:\ChatP2PData\.mesh
python -m chatp2p.cli node uninstall-task --task-name "ChatP2P Worker Watchdog" --home E:\ChatP2P-private-version--main\.runtime\.mesh
```

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
