# CyberGym Claude Opus 5 pass@3

## What you need

- A fresh Ubuntu 24.04 x86_64 EC2 instance. Recommended: `m7i.16xlarge`,
  1 TiB gp3 disk, and an Elastic IP.
- Security-group rules for SSH from your IP and public TCP 80/443.
- These three API keys:
  - `HUD_API_KEY`
  - `ANTHROPIC_API_KEY`
  - `DAYTONA_API_KEY`

## Install

SSH into the EC2 instance and run:

```bash
curl -fsSL https://install.determinate.systems/nix | sh -s -- install --no-confirm
. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh

git clone https://github.com/hud-evals/cybergym-opus5-handoff.git cybergym
cd cybergym
nix run .#bootstrap
```

`bootstrap` prompts privately for the three keys. It installs Docker, downloads
and verifies the complete benchmark and grader, builds the pinned agent, starts
the private grader and task-scoped HTTPS relay, runs all no-model preflight
checks, and installs the durable Daytona workers. Rerunning it safely resumes
partial downloads and preserves existing keys and checkpoints.

## Run

```bash
# Start all three paid repeats
nix run .#daytona -- start

# Show every lane's progress
nix run .#daytona -- status

# Let active shards finish, checkpoint them, and launch nothing new
nix run .#daytona -- pause

# Reconcile saved receipts/sandboxes and continue pending work
nix run .#daytona -- resume
```

Pause never kills an active rollout. Resume never reruns a verified task and
cannot resume in the middle of a model turn. Results, raw trajectories,
projections, grader summaries, sandbox ledgers, and campaign manifests remain
on the EC2 disk and completed job scores are uploaded to HUD.

Each lane completes repeat 1, repeat 2, and repeat 3 in separate durable state
directories. The last finishing lane automatically writes the strict local
4,521-row ledger to
`/srv/cybergym/results-og-fidelity/opus5-pass3/final-hud-reported-4521.json`
and the aggregate pass@3 report to
`/srv/cybergym/results-og-fidelity/opus5-pass3/final-pass-at-3.json`. The
finalizer can also be rerun safely with `nix run .#finalize-pass3`.

## Rotate keys

```bash
sudo -E nix run .#update-keys
nix run .#daytona-ready
```
