# Provider Edge Mode

Provider edge mode is an ISP-edge / broadband-bundle simulation. It does not put AI inside fibre, routers, or cables. It models how a broadband provider could run a ChatP2P coordinator, let subscribers contribute light local nodes, add regional edge workers, and use trusted peer workers when local/provider capacity is not suitable.

This is a proof harness, not a real ISP deployment, billing system, or crypto/payment system.

## Roles

- `coordinator`: provider-run coordinator for signed jobs, leases, results, and verification.
- `subscriber_gateway`: household/router-style node for a subscriber.
- `subscriber_device`: a subscriber device node.
- `provider_edge_worker`: stronger regional worker operated by the provider.
- `contributor_worker`: trusted external peer worker.
- `verifier`: worker used to produce duplicate results for deterministic verification.

Existing alpha workers continue to work. Provider roles are advertised as capability metadata on normal signed node registration.

## Bootstrap

Create a provider config:

```powershell
python -m chatp2p.cli operator bootstrap-provider `
  --config D:\ChatP2PData\provider-config.json `
  --provider-name "Demo Fibre AI" `
  --region "Hull"
```

Add a subscriber:

```powershell
python -m chatp2p.cli provider create-subscriber `
  --config D:\ChatP2PData\provider-config.json `
  --subscriber-id sub_demo_001 `
  --plan "Broadband AI Plus"
```

Create a provider-mode node profile:

```powershell
python -m chatp2p.cli node join-provider `
  --provider-config D:\ChatP2PData\provider-config.json `
  --subscriber-id sub_demo_001 `
  --home D:\ChatP2PData\.mesh-provider-sub-001
```

The join-provider command writes a normal worker identity and `node-capabilities.json` with provider role metadata. It does not expose ports, alter firewall settings, or start public networking.

## Proof

Run the local proof harness:

```powershell
python -m chatp2p.cli proof provider-edge `
  --provider-config D:\ChatP2PData\provider-config.json `
  --subscribers 3 `
  --edge-workers 1 `
  --jobs 25 `
  --report D:\ChatP2PData\provider-edge-proof.json
```

The proof creates a local coordinator, simulated subscriber gateway nodes, provider edge workers, peer workers, and verifier workers. It creates signed deterministic jobs for subscribers, selects a route by policy, runs signed lease acknowledgements and signed results, verifies jobs with duplicate results, updates a simple credit summary, and writes a JSON report.

Pass means:

- all proof-created jobs verified
- disputes are `0`
- expired jobs are `0`
- fallback placeholder routes are `0`
- route counts include local, provider edge, and peer paths in the happy path
- subscriber spend and worker/provider credit movement are recorded

## What Is Not Proven Yet

- real ISP deployment
- real routers or broadband hardware
- real payment, billing, tokens, or crypto
- public internet exposure
- geographic latency or edge placement
- production privacy isolation

The point is to make the broadband-provider architecture measurable before touching any real network deployment work.

## Ops Pack

Use the ops pack command when you want a repeatable folder that can be reviewed or shared without copying runtime homes, identity files, SQLite databases, or private alpha invite files:

```powershell
python -m chatp2p.cli operator provider-ops-pack `
  --provider-config D:\ChatP2PData\provider-config.json `
  --out D:\ChatP2PData\provider-ops-pack `
  --subscribers 3 `
  --edge-workers 1 `
  --jobs 25
```

The pack writes:

- `provider-edge-proof.json`
- `provider-ops-pack-summary.json`
- `provider-ops-pack-summary.md`
- `provider-handoff.md`
- `D:\ChatP2PData\provider-ops-pack.zip`

Pass means the underlying provider proof passed, disputes stayed at `0`, no job routed to `fallback_placeholder`, and the zip was created unless `--no-zip` was used.

## Live Alpha Bridge

Provider metadata can be advertised by real alpha workers without changing the signed registration protocol. Refresh a worker capability profile with a provider role and restart it:

```powershell
python -m chatp2p.cli node refresh-capabilities `
  --home D:\ChatP2PData\.mesh `
  --invite D:\ChatP2PData\alpha-invite.json `
  --provider-config D:\ChatP2PData\provider-config.json `
  --node-role provider_edge_worker `
  --restart-worker `
  --report D:\ChatP2PData\provider-role-refresh-report.json
```

For a trusted outside worker, use `--node-role contributor_worker` on their machine after they pull the latest code and have the same provider config file.

Then run a live provider-shaped proof against the alpha coordinator:

```powershell
python -m chatp2p.cli operator provider-remote-proof `
  --invite D:\ChatP2PData\alpha-invite.json `
  --provider-config D:\ChatP2PData\provider-config.json `
  --expected-worker-id worker_87b5cefe53e67c6c `
  --jobs 10 `
  --report D:\ChatP2PData\provider-remote-proof.json
```

The report schema is `chatp2p.provider-remote-proof-report.v1`. It records requested provider routes, actual result routes inferred from worker roles, result counts per node, expected-worker contribution, provider snapshot stats, and the final coordinator snapshot. This proves provider-shaped work can run across the existing alpha coordinator-worker network.
