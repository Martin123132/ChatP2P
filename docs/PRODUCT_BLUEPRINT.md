# Product Blueprint

## Product Name

Working name: **ChatP2P**

One-line pitch:

> A peer-to-peer network where ordinary computers contribute verified AI work: inference, evaluation, dataset review, model feedback, and eventually distributed fine-tuning.

This project should not start as "an unbiased AI that belongs to humanity." That is a mission. The product is simpler:

> Install a node, donate spare compute, earn usage credits and reputation, and help decide what the network learns.

## The Real Problem

AI capability is becoming concentrated inside a few companies because training, serving, data selection, and feedback loops are centralized.

The counter-product is not a local chatbot. It is a network that lets many machines do different useful jobs, then combines the verified results into a shared intelligence layer.

## What A Node Does

Every participant runs a node. The node benchmarks the machine and advertises which jobs it can safely accept.

Node capabilities:

- **Inference worker**: runs local open models and serves requests.
- **Evaluation worker**: tests model answers against benchmarks, rubrics, and adversarial prompts.
- **Data worker**: reviews, labels, deduplicates, or rejects training data.
- **Rollout worker**: generates candidate answers for RL, preference collection, and model comparison.
- **Fine-tune worker**: trains small adapters such as LoRA modules on approved datasets.
- **Verification worker**: repeats jobs or checks outputs to detect bad results, spam, or poisoning.
- **Coordinator node**: routes jobs, aggregates results, tracks reputation, and publishes network state.

This is the "different computers doing different work" model. A phone should not be treated like an H100. A phone can vote, review, run small evals, and act as a light client. A gaming laptop can run inference, generate rollouts, and train small adapters. Multi-GPU rigs can handle heavier training jobs.

## What The MVP Is

The first shippable product should be a desktop node app and a public dashboard.

MVP scope:

1. One-click node install for Windows and Linux.
2. Local benchmark: CPU, GPU, VRAM, RAM, disk, network, power limits.
3. Job router that sends small verified jobs to nodes.
4. Credit ledger: contribute compute, earn usage credits.
5. Inference marketplace for a small set of open models.
6. Evaluation jobs where multiple nodes score the same model output.
7. Public dashboard showing live nodes, jobs completed, model scores, and disputed results.

Do not begin with full decentralized pretraining. Begin with useful work the network can verify.

## Credit Ledger

Credits are the usage accounting layer for the MVP. They are not money, crypto, or a cash-out promise.

V1 records auditable ledger entries for accepted worker results:

- account id and account type
- positive or negative credit delta
- balance after the transaction
- reason, such as `worker_result_reward`
- job id, node id, output hash, and small safe metadata

The current node `credits` balance remains available for old clients, but the ledger is the source to build future spend, reserve, refund, and dispute flows. The next inference marketplace milestone should add requester accounts and job-cost reservation before a chat job is leased.

Requester Reservation V1 adds that first spend path: a job may declare `requester_account_id` and `job_cost`, the coordinator debits `job_cost_reserved` before the job is queued, and accepted worker output still earns a separate `worker_result_reward`. Dispute settlement remains future work.

Requester Refund V1 closes the simplest bad-spend case: if a funded job expires without any accepted result, the coordinator adds `job_cost_refunded` and returns the reserved credits. Refunds are ledger entries rather than history rewrites, so reservation, failure, and settlement stay visible.

Chat Inference V1 uses `inference.chat.v1` as the first real product loop for credits. A requester reserves credits, the coordinator routes the chat job to an Ollama-capable worker with the requested model, and the accepted answer earns the worker reward. This is the bridge between the ledger and the future chat UI; it is still local-model infrastructure, not a hosted model marketplace yet.

Funded Chat Smoke V1 turns that loop into one repeatable operator proof. In default fake-Ollama mode it needs no partner node and no model download: grant requester credits, reserve job cost, lease a signed chat job, submit a signed answer, reward the worker, and write `chatp2p.funded-chat-smoke-report.v1`. Real local Ollama mode is available when the operator wants to test an installed open model.

Chat Ask V1 is the requester-facing counterpart. It submits a funded `inference.chat.v1` job to a running coordinator, waits for a worker answer, and writes a local transcript/report without exposing invite tokens. This is still CLI-first, but it is the first shape of the future chat app: one prompt, one reserved credit spend, one contributed-compute answer.

Chat Session V1 makes that loop persistent. Each CLI run appends one funded turn to a local session transcript, sends recent verified turns as bounded context, preserves per-turn `chat-ask` evidence, and records requester balance after the spend. This is the first concrete bridge from operator proofs into a future chat UI.

Chat Session Status/Resume V1 adds the operator safety loop around that transcript: inspect local session health without spending credits, identify failed turns, and append auditable retry turns only when explicitly requested. Submitted turns remain protected from accidental duplicate spend unless the operator opts in.

Operator Credit Tools V1 adds the missing controlled top-up path. Operators can inspect requester and worker balances with a read-only `operator credits` report, then grant requester credits with `operator grant-requester-credits` through a separate operator-only grant token. The normal alpha invite/admission token can still create jobs, but it cannot mint credits.

## Future Lane: ISP Edge / Broadband Bundle

Keep a second product architecture lane for an ISP-edge simulation, but do not let it interrupt the current alpha path. The idea is not "AI inside fibre"; it is a broadband-provider-style deployment model where a provider runs a coordinator, subscribers run light gateway/device nodes, regional edge workers provide stronger capacity, and policy routes work through local, provider-edge, trusted-peer, then placeholder fallback paths.

The measurable future proof should cover provider config, subscriber creation, provider edge workers, route counts, credit movement, signed results, verification, and a JSON evidence report. No real billing, crypto, or ISP deployment claims belong in that milestone.

## Job Packet Format

Every unit of work should be a signed job packet.

Required fields:

- `job_id`
- `job_type`
- `model_id`
- `input_hash`
- `payload_uri`
- `resource_requirements`
- `deadline`
- `expected_output_schema`
- `verification_strategy`
- `reward`
- `coordinator_signature`

Output fields:

- `job_id`
- `node_id`
- `output_hash`
- `metrics`
- `runtime`
- `hardware_attestation`
- `worker_signature`

The network should avoid trusting a single machine. Important jobs should be repeated or checked by independent workers.

## Verification Model

Verification should be practical before it is perfect.

For inference:

- duplicate a percentage of jobs across multiple nodes
- compare output hashes when deterministic
- use judge models and human review when non-deterministic
- track latency, refusal rates, hallucination reports, and user ratings

For evals:

- require multiple independent scorers
- measure scorer agreement
- down-rank nodes that consistently disagree without good reason

For data:

- separate submitters from reviewers
- require provenance metadata
- reject unclear license status
- record dissent rather than forcing fake consensus

For training:

- start with LoRA/adapters
- aggregate only signed updates
- test every submitted adapter against public evals before promotion
- quarantine suspicious updates

## Incentives

The network needs incentives that are not vague.

Credits:

- users spend credits to run inference or training jobs
- nodes earn credits for accepted work
- credits are not a promise of money in the MVP

Reputation:

- accuracy score
- uptime score
- verification score
- domain expertise badges
- dispute history

Governance:

- high-reputation contributors can propose models, datasets, evals, and policies
- controversial decisions require visible evidence and recorded dissent

## Governance Surface

The first governance product should be boring and strict:

- dataset proposal
- model proposal
- eval proposal
- policy proposal
- security report
- dispute ticket

Each proposal must include:

- motivation
- evidence
- risks
- test plan
- rollback plan
- affected users

## Repository Direction

The current repo already contains the seed of the system:

- domain nodes: math, language, philosophy, humor, ethics
- validation nodes: theory testing, deception/truth, epistemic humility
- memory nodes: revision history and confidence tracking

The missing layer is the runtime:

- node daemon
- job router
- signed job packets
- node registry
- reputation store
- dashboard
- contribution workflow

## Technical MVP Architecture

Suggested stack:

- Node client: Python or Rust daemon with a local web UI
- Desktop wrapper later: Tauri
- API: FastAPI for the first coordinator
- P2P later: libp2p or similar peer discovery
- Model runtime: llama.cpp, Ollama, vLLM where hardware allows
- Storage: SQLite locally, Postgres for the first coordinator
- Signatures: Ed25519 node identity keys
- Dashboard: React or Next.js

The first version can have a coordinator. The important thing is to design the protocol so the coordinator can be replaced or multiplied later.

## 90-Day Build Plan

Days 1-15:

- turn example nodes into real Python modules
- add a CLI: `mesh node start`
- benchmark local hardware
- generate node identity keys

Days 16-30:

- define job packet schema
- build coordinator API
- submit and complete eval jobs
- store signed outputs

Days 31-45:

- add inference jobs through a small local model
- duplicate jobs for verification
- build reputation scoring v0

Days 46-60:

- add public dashboard
- show live nodes, completed jobs, failed jobs, and scores
- add contribution docs

Days 61-75:

- add data review jobs
- add dataset proposal workflow
- add dispute tickets

Days 76-90:

- run a closed alpha with 10-50 nodes
- publish benchmark results
- harden security boundaries
- write the first technical report

## First Proof

The first proof should be narrow:

> Can 25 random machines reliably run and verify evaluation jobs for open models better than a single central benchmark script?

If yes, expand to inference.

If inference works, expand to data review.

If data review works, expand to LoRA fine-tuning.

That path gets the idea off the ground without pretending the first milestone is a frontier model.
