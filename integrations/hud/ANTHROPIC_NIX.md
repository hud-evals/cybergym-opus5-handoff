# CyberGym Claude Opus 5 Nix handoff

Use a native Linux x86_64 host with Docker and KVM-capable virtualization where
required. The branch is `agent/cybergym-daytona-anthropic`; it is separate from
the running GPT-5.6 campaign.

```sh
git switch agent/cybergym-daytona-anthropic
nix develop
nix run .#setup
sudo -E nix run .#configure
nix run .#preflight
nix run .#daytona-preflight
```

The configure step prompts privately for `HUD_API_KEY`,
`ANTHROPIC_API_KEY`, and `DAYTONA_API_KEY`, then rotates the internal CyberGym
credential. The resulting protected files live outside the checkout under
`/etc/cybergym`. No secret belongs in `flake.nix`, `flake.lock`, Git, shell
history, or a HUD trace.

Run one paid smoke only after preflight passes:

```sh
nix run .#smoke -- --confirm-spend
```

Run the restart-safe full catalog only after reviewing the smoke and explicit
spend boundary:

```sh
nix run .#campaign -- --confirm-paid-all --continue-after-errors
```

For the private Daytona lane, set the non-secret relay/task-file paths described
by `integrations/hud/ops/daytona-campaign.sh`, then run:

```sh
nix run .#daytona-campaign -- --confirm-paid-selection
```

The model arm and HUD Job identity are fixed to direct `claude-opus-5` and
`cybergym-claude-opus-5-no-internet-v1`. Model access occurs in the trusted
coordinator. The task runtime has no public IP/DNS egress and can reach only
the task-scoped submission relay. Terminal infrastructure-error rows remain
pending for exact automatic retry after their remote receipt is reconciled.
