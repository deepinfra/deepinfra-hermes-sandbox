# deepinfra-hermes-sandbox

A [hermes-agent](https://github.com/NousResearch/hermes-agent) `terminal.backend` plugin backed by [DeepInfra Sandboxes](https://docs.deepinfra.com/sandboxes/overview) (`deep_sands`) — isolated cloud microVMs reached only via server-side exec, with no network access to the sandbox at all.

Registers via hermes-agent's `TerminalEnvironmentProvider` plugin ABI ([hermes-agent PR #94400](https://github.com/NousResearch/hermes-agent/pull/94400)) — this is how third-party cloud sandbox vendors integrate with hermes-agent without needing changes to hermes-agent core (see hermes-agent's `AGENTS.md`, "No new third-party-product plugins in-tree"). The Sprites cloud-sandbox integration ([PR #93523](https://github.com/NousResearch/hermes-agent/pull/93523)) was closed unmerged in favor of this same extension point, then re-shipped as a standalone plugin at [NousResearch/hermes-plugin-sprites](https://github.com/NousResearch/hermes-plugin-sprites) — that repo is the actual precedent.

## Install

Not yet published to PyPI — install straight from GitHub for now:

```bash
pip install git+https://github.com/deepinfra/deepinfra-hermes-sandbox
hermes plugins enable deepinfra-sandbox
hermes config set terminal.backend deepinfra
```

Once published, `pip install deepinfra-hermes-sandbox` will work in place of the `git+https://...` form above.

Note the plugin/enable name is `deepinfra-sandbox`, not `deepinfra` — hermes-agent already bundles two unrelated plugins literally named `deepinfra` (its image-gen and video-gen backends), and `hermes plugins enable deepinfra` would silently enable one of those instead. `terminal.backend` is a separate config value and stays `deepinfra`.

`DEEPINFRA_API_KEY` is required — the same account-level key already used for DeepInfra LLM/image/video inference, if you have that configured. Get one at [deepinfra.com/dash/api_keys](https://deepinfra.com/dash/api_keys).

Run `hermes doctor` to confirm the SDK and key are both detected.

## What this gives you

| Surface | Behavior |
|---|---|
| `terminal` / `execute_code` / file tools | Commands run in a fresh deep_sands sandbox; `/workspace` is the only path that survives a stop/start cycle |
| Per-turn persistence | A sandbox survives between turns *within one session* (`terminal.container_persistent: true`, the default). When hermes-agent's idle reaper fires (`terminal.lifetime_seconds`, default 300s of inactivity), the sandbox is stopped (not terminated) and resumed automatically on the next command — `/workspace` survives. Terminated for real at session end. No cross-session resume yet (unlike Daytona's default) — a brand new session always starts fresh |
| Dangerous-command approval | Skipped — deep_sands sandboxes are fully isolated, no host paths are ever mounted in |
| File sync | Credentials/skills/cache are pushed to `/workspace/.hermes`; only the subset of those same files that changed remotely is pulled back on cleanup (hermes-agent core's `FileSyncManager.sync_back`, hash-compared against what was pushed — not a general bidirectional sync of anything else written under that path). Bulk transfers use a self-built tar-and-exec workaround (deep_sands has no native batch transfer API yet) |
| Interrupt | Stops the whole sandbox (deep_sands has no per-command cancel); the next command transparently resumes it |

## Known limitations (v1)

- No custom sandbox image — deep_sands doesn't support one (deliberately removed by DeepInfra in favor of a fixed, toolchain-baked base image).
- No automatic plan-tier sizing — `container_cpu`/`container_memory` are not consulted: hermes-agent core defaults both for *every* backend whether or not a user actually customized them, and naively fitting that shared default into deep_sands' plan catalog was found (against the live API) to silently pick a plan tier at roughly 2x the cost of the platform's own default for anyone who never touched sizing. Instead, set the `DEEPINFRA_SANDBOX_PLAN` environment variable (e.g. `DEEPINFRA_SANDBOX_PLAN=large`) to explicitly opt into a non-default tier; leave it unset to get deep_sands' own default plan. This is an unambiguous, deliberate opt-in rather than an inferred one — revisit once hermes-agent core can distinguish "requested" `container_cpu`/`container_memory` from "defaulted" ones (see [hermes-agent PR #96161](https://github.com/NousResearch/hermes-agent/pull/96161), open).
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
