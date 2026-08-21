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
  https://github.com/hud-evals/cybergym.git
cd cybergym

nix run .#bootstrap
```

`bootstrap` privately prompts for the three keys. It installs Docker, downloads
and verifies the complete benchmark and grader, builds the pinned agent, starts
the private grader and HTTPS relay, runs the no-model checks, and installs the
reboot-resilient Daytona workers. Rerunning it preserves keys and checkpoints.

## Start or control the run

```bash
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
sudo -E nix run .#update-keys
nix run .#daytona-ready
```
