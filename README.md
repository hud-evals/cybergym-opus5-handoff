# CyberGym Claude Opus 5 pass@3

This branch is the supported handoff for running Claude Opus 5 on all 1,507
CyberGym tasks three times: 4,521 complete HUD trajectories.

## What you need

- A fresh Ubuntu 24.04 x86_64 EC2 instance. Recommended: `m7i.16xlarge`, a
  1-TiB gp3 disk, and an Elastic IP.
- Inbound SSH from the operator and public TCP 80/443 for the task-scoped HTTPS
  relay. Allow outbound HTTPS.
- These three keys:
  - `HUD_API_KEY`
  - `DAYTONA_API_KEY`
  - `ANTHROPIC_API_KEY`

Task machines run remotely on Daytona. The EC2 coordinator stores the benchmark
corpus, private grader, durable campaign state, and final reports.

## Fresh EC2 setup

SSH into the instance as the normal Ubuntu user and run:

```bash
sudo apt-get update
sudo apt-get install -y curl git

curl --proto '=https' --tlsv1.2 -sSf -L \
  https://install.determinate.systems/nix | \
  sh -s -- install linux --no-confirm

. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh

git clone --branch agent/nix-opus-5-handoff \
  https://github.com/hud-evals/cybergym-opus5-handoff.git cybergym
cd cybergym

nix run .#bootstrap-session
```

`bootstrap-session` opens a named `tmux` session and runs the resumable
`bootstrap` command inside it. It privately prompts for the three keys,
installs Docker, downloads and verifies the complete benchmark and grader,
builds the pinned agent, starts the private grader and HTTPS relay, runs the
no-model checks, and installs the reboot-resilient Daytona workers. If SSH
disconnects, rerun `nix run .#bootstrap-session` to reattach; the download and
build continue on the host without manual recovery. If bootstrap exits with an
error, rerunning the same command starts an idempotent retry from its saved
files and checkpoints.

The agent budget follows the published CyberGym OpenHands setup: at most 100
agent iterations and 2,048 output tokens per model turn. This is harness-budget
parity, not a reproduction of the paper's model cohort: the model evaluated by
this branch is Claude Opus 5.

## Start or control the run

```bash
nix run .#daytona-canary -- --confirm-spend YES  # one isolated paid validation task
nix run .#daytona -- start
nix run .#daytona -- status
nix run .#daytona -- pause
nix run .#daytona -- resume
```

The campaign automatically:

- Finishes and seals all 1,507 attempt-1 rows before admitting attempt 2, then
  does the same before attempt 3.
- Checkpoints every task/attempt cell and never reruns a verified result.
- Lets active work finish when paused and resumes from exact saved receipts.
- Reconciles owned Daytona sandboxes before retrying infrastructure failures.
- Stops fleet admission on exhausted Anthropic credit and uses one bounded
  recovery probe instead of a restart storm.
- Records explicit Anthropic safety refusals as terminal score `0` without a
  retry.
- Keeps benchmark task sandboxes off the public Internet.

## Finish the pulled pass@1 cohort only

The verified Opus 5 HUD pull contains valid completed numeric results for 89 of
the 1,507 tasks. The checked-in scored complement contains the other 1,418
tasks in canonical catalog order:

```text
integrations/hud/ops/opus5-pass1-completed-from-pull.txt
integrations/hud/ops/opus5-missing-pass1-tasks.txt
```

After `bootstrap` and `daytona-ready`, run exactly the missing pass@1 tasks with:

```bash
nix run .#run-missing-pass1 -- --confirm-spend YES
```

This selected campaign checkpoints locally and is safe to resume with the same
command. Error, partial, and empty pulled traces remain in the missing list.

After pass@1 finishes and its scores are visible on HUD, run only the two
remaining full-catalog rounds with:

```bash
nix run .#continue-pass3 -- --confirm-spend YES
```

This command never starts pass@1. It runs round 2 as
`cybergym-opus5-cyber-pass-2`, then round 3 as
`cybergym-opus5-cyber-pass-3`, with separate restart-safe local state.

## Results

The finalizer runs automatically after the last attempt. It can also be safely
rerun without a model call:

```bash
nix run .#finalize-pass3
```

Private state and final reports are stored under:

```text
/srv/cybergym/results-og-fidelity/opus5-pass3/
/srv/cybergym/results-og-fidelity/opus5-pass3/final-hud-reported-4521.json
/srv/cybergym/results-og-fidelity/opus5-pass3/final-pass-at-3.json
```

If setup or execution stops, inspect without deleting saved state:

```bash
nix run .#daytona -- status
sudo systemctl --failed
sudo journalctl 'cybergym-daytona@*' -n 200 --no-pager
```

Rotate protected keys with:

```bash
nix run .#update-keys
nix run .#daytona-ready
```
