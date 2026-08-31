"""DeepInfra sandbox terminal backend -- a standalone hermes-agent plugin.

Registers deep_sands (DeepInfra's cloud sandbox product) as a
``terminal.backend`` option via the TerminalEnvironmentProvider plugin ABI
(agent/terminal_env_provider.py, introduced in hermes-agent PR #94400
specifically so third-party cloud sandboxes can ship without touching core
-- see that PR's own description and the Sprites backend's move to a
standalone plugin repo, #93523, for the precedent this follows).

Install:
    pip install git+https://github.com/deepinfra/deepinfra-hermes-sandbox
    hermes plugins enable deepinfra-sandbox
    hermes config set terminal.backend deepinfra

DEEPINFRA_API_KEY is the same account-level key already used by DeepInfra's
existing model-provider/image-gen/video-gen integrations for LLM/image/video
inference -- no separate credential flow needed here.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
from typing import Any, Dict, List, Optional, Tuple

from agent.secret_scope import get_secret
from agent.terminal_env_provider import TerminalEnvironmentProvider

from deepinfra_hermes_sandbox.environment import CACHE_PATH_BASE, DeepInfraEnvironment

try:
    # Single source of truth is pyproject.toml's `version` -- read back from
    # the installed distribution's metadata instead of hardcoding a second
    # copy here that can drift out of sync.
    __version__ = importlib.metadata.version("deepinfra-hermes-sandbox")
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.0.0+unknown"


class DeepInfraProvider(TerminalEnvironmentProvider):
    name = "deepinfra"
    display_name = "DeepInfra Sandboxes"
    is_remote = True
    is_container = True
    # skip_container_guards defaults to is_container (True) on the base
    # class -- correct here, since deep_sands sandboxes are fully isolated
    # server-side k8s pod-exec with no network access to the sandbox and no
    # host paths ever mounted in.
    session_isolated_when_nonpersistent = False  # v1 is ephemeral-only, no by-name resume yet

    @property
    def description(self) -> str:
        return "Run commands in a DeepInfra cloud sandbox (deep_sands)."

    @property
    def cache_path_base(self) -> Optional[str]:
        return CACHE_PATH_BASE

    @property
    def strip_env_keys(self) -> frozenset:
        return frozenset({"DEEPINFRA_API_KEY"})

    @property
    def env_description(self) -> str:
        return "a DeepInfra cloud sandbox (Linux)"

    def is_available(self) -> bool:
        return (
            importlib.util.find_spec("deepinfra") is not None
            and bool((get_secret("DEEPINFRA_API_KEY", "") or "").strip())
        )

    def setup_instructions(self) -> List[str]:
        return [
            "pip install git+https://github.com/deepinfra/deepinfra-hermes-sandbox",
            "hermes plugins enable deepinfra-sandbox  # not \"deepinfra\" -- "
            "that name collides with the bundled image/video-gen plugins",
            "Get an API key at https://deepinfra.com/dash/api_keys "
            "(same key used for LLM inference, if already configured)",
            "Set DEEPINFRA_API_KEY in your environment or ~/.hermes/.env",
            "Optional: set DEEPINFRA_SANDBOX_PLAN (e.g. \"large\") to pick a "
            "plan tier -- defaults to deep_sands' own default plan otherwise",
        ]

    def doctor_checks(self) -> List[Tuple[bool, str, str]]:
        sdk_installed = importlib.util.find_spec("deepinfra") is not None
        has_key = bool((get_secret("DEEPINFRA_API_KEY", "") or "").strip())
        return [
            (
                sdk_installed,
                "DeepInfra SDK installed",
                "(deepinfra package)" if sdk_installed
                else "(missing -- pip install git+https://github.com/deepinfra/deepinfra-hermes-sandbox)",
            ),
            (
                has_key,
                "DEEPINFRA_API_KEY configured",
                "(configured)" if has_key
                else "(not set -- see https://deepinfra.com/dash/api_keys)",
            ),
        ]

    def create_environment(
        self,
        *,
        cwd: str,
        timeout: int,
        task_id: str = "default",
        image: Optional[str] = None,
        container_config: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        # `image` is always ignored -- deep_sands has no custom-image
        # concept, every sandbox runs a fixed platform base image.
        cc = container_config or {}
        return DeepInfraEnvironment(
            cwd=cwd,
            timeout=timeout,
            task_id=task_id,
            persistent_filesystem=bool(cc.get("container_persistent", True)),
        )


def register(ctx) -> None:
    """Plugin entry point -- wire DeepInfraProvider into the terminal-env registry."""
    ctx.register_terminal_environment_provider(DeepInfraProvider())
