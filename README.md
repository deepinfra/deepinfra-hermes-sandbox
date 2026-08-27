# deepinfra-hermes-sandbox

A [hermes-agent](https://github.com/NousResearch/hermes-agent) `terminal.backend` plugin backed by [DeepInfra Sandboxes](https://docs.deepinfra.com/sandboxes/overview) (`deep_sands`) — isolated cloud microVMs reached only via server-side exec, with no network access to the sandbox at all.

Registers via hermes-agent's `TerminalEnvironmentProvider` plugin ABI ([hermes-agent PR #94400](https://github.com/NousResearch/hermes-agent/pull/94400)), the same extension point the [Sprites backend](https://github.com/NousResearch/hermes-agent/pull/93523) uses — this is how third-party cloud sandbox vendors integrate with hermes-agent without needing changes to hermes-agent core (see hermes-agent's `AGENTS.md`, "No new third-party-product plugins in-tree").

## Install

```bash
pip install deepinfra-hermes-sandbox
hermes plugins enable deepinfra
hermes config set terminal.backend deepinfra
```

`DEEPINFRA_API_KEY` is required — the same account-level key already used for DeepInfra LLM/image/video inference, if you have that configured. Get one at [deepinfra.com/dash/api_keys](https://deepinfra.com/dash/api_keys).

Run `hermes doctor` to confirm the SDK and key are both detected.

## What this gives you

| Surface | Behavior |
|---|---|
| `terminal` / `execute_code` / file tools | Commands run in a fresh deep_sands sandbox; `/workspace` is the only path that survives a stop/start cycle |
| Per-turn persistence | A sandbox survives between turns *within one session* (`terminal.container_persistent: true`, the default) and is idle-reaped / terminated at session end. No cross-session resume yet (unlike Daytona's default) — every new session starts fresh |
| Dangerous-command approval | Skipped — deep_sands sandboxes are fully isolated, no host paths are ever mounted in |
| File sync | Credentials/skills/cache are synced to `/workspace/.hermes`; bulk transfers use a self-built tar-and-exec workaround (deep_sands has no native batch transfer API yet) |
| Interrupt | Stops the whole sandbox (deep_sands has no per-command cancel); the next command transparently resumes it |

## Known limitations (v1)

- No custom sandbox image — deep_sands doesn't support one (deliberately removed by DeepInfra in favor of a fixed, toolchain-baked base image).
- No plan-tier sizing — every sandbox uses deep_sands' own default plan. `container_cpu`/`container_memory` are not consulted: hermes-agent core defaults both for *every* backend whether or not a user actually customized them, and naively fitting that shared default into deep_sands' plan catalog was found (against the live API) to silently pick a plan tier at roughly 2x the cost of the platform's own default for anyone who never touched sizing. Fixed by not auto-selecting a tier at all; revisit once there's a reliable way to distinguish "requested" from "defaulted."
- 30-minute hard cap per command (deep_sands server-side, not client-enforced).
- No stdin piping — heredoc-only.
- Per-account cap of 5 concurrent sandboxes.
- No cross-session persistence yet.

## Running tests

Tests exercise a plugin that runs inside a hermes-agent process, so they need hermes-agent's own packages (`tools.*`, `agent.*`) importable:

```bash
git clone https://github.com/NousResearch/hermes-agent
pip install -e ./hermes-agent
pip install -e .[dev]
pytest tests/
```

Live-API tests are not included in this package's `tests/` directory (the mocked SDK suite doesn't need real credentials); manual end-to-end verification against the real deep_sands API is documented in this repo's PR history.

## License

MIT
