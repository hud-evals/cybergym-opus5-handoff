# CyberGym Claude Opus 5 pass@3 Nix handoff

The authoritative fresh-EC2 instructions are in the repository root
[`README.md`](../../README.md#claude-opus-5-nixdaytona-handoff).

The complete setup is:

```sh
nix run .#bootstrap
nix run .#daytona -- start
```

`bootstrap` prompts privately for `HUD_API_KEY`, `ANTHROPIC_API_KEY`, and
`DAYTONA_API_KEY`, downloads and verifies every pinned public artifact, and
performs all no-inference gates. It does not depend on VM101, Tailscale, AWS
Systems Manager, or access to HUD infrastructure.

Fleet lifecycle commands are:

```sh
nix run .#daytona -- status
nix run .#daytona -- pause
nix run .#daytona -- resume
```

Pause is applied at shard boundaries. Completed tasks, HUD receipts, raw
trajectories, projections, grader summaries, and sandbox ledgers remain in the
lane's durable state and result directories. Resume reconciles those records
before it starts pending work.

The 24 services each own three repeat-scoped manifests. Completion produces an
exact local 1,507-task x 3-repeat ledger and `final-pass-at-3.json`; malformed,
missing, nonnumeric, or infrastructure-error grades are rejected rather than
counted. `nix run .#finalize-pass3` safely recomputes the report without a model
call or HUD polling.
