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
