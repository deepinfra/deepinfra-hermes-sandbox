"""Unit tests for DeepInfraProvider and its registration into hermes-agent's
terminal-environment-provider registry.
"""

from unittest.mock import MagicMock

import pytest

from agent import terminal_env_registry as reg
from deepinfra_hermes_sandbox import DeepInfraProvider


@pytest.fixture(autouse=True)
def _clean_registry():
    reg._reset_for_tests()
    yield
    reg._reset_for_tests()


class TestProviderIdentity:
    def test_name_and_flags(self):
        p = DeepInfraProvider()
        assert p.name == "deepinfra"
        assert p.is_remote is True
        assert p.is_container is True
        assert p.skip_container_guards is True  # defaults to is_container
        assert p.session_isolated_when_nonpersistent is False

    def test_cache_path_base(self):
        p = DeepInfraProvider()
        assert p.cache_path_base == "/workspace/.hermes"

    def test_strip_env_keys(self):
        p = DeepInfraProvider()
        assert p.strip_env_keys == frozenset({"DEEPINFRA_API_KEY"})


class TestAvailability:
    def test_unavailable_without_key(self, monkeypatch):
        monkeypatch.setattr(
            "deepinfra_hermes_sandbox.get_secret", lambda name, default=None: None
        )
        assert DeepInfraProvider().is_available() is False

    def test_available_with_key_and_sdk(self, monkeypatch):
        monkeypatch.setattr(
            "deepinfra_hermes_sandbox.get_secret",
            lambda name, default=None: "sk-test" if name == "DEEPINFRA_API_KEY" else default,
        )
        monkeypatch.setattr(
            "deepinfra_hermes_sandbox.importlib.util.find_spec",
            lambda name: object() if name == "deepinfra" else None,
        )
        assert DeepInfraProvider().is_available() is True

    def test_doctor_checks_report_both_rows(self, monkeypatch):
        monkeypatch.setattr(
            "deepinfra_hermes_sandbox.get_secret", lambda name, default=None: None
        )
        monkeypatch.setattr(
            "deepinfra_hermes_sandbox.importlib.util.find_spec", lambda name: None
        )
        rows = DeepInfraProvider().doctor_checks()
        assert len(rows) == 2
        assert all(ok is False for ok, _, _ in rows)


class TestCreateEnvironment:
    def test_ignores_image_and_threads_persistence(self, monkeypatch):
        mock_env = MagicMock()
        mock_ctor = MagicMock(return_value=mock_env)
        monkeypatch.setattr(
            "deepinfra_hermes_sandbox.DeepInfraEnvironment", mock_ctor
        )

        p = DeepInfraProvider()
        result = p.create_environment(
            cwd="/workspace", timeout=60, task_id="t1",
            image="ignored:latest",
            container_config={"container_persistent": False},
        )

        assert result is mock_env
        _, kwargs = mock_ctor.call_args
        assert kwargs["cwd"] == "/workspace"
        assert kwargs["timeout"] == 60
        assert kwargs["task_id"] == "t1"
        assert kwargs["persistent_filesystem"] is False
        assert "image" not in kwargs
        assert "plan" not in kwargs


class TestRegistryIntegration:
    def test_registered_flags_readable_via_provider_flag(self):
        reg.register_provider(DeepInfraProvider())

        assert reg.provider_flag("deepinfra", "is_remote") is True
        assert reg.provider_flag("deepinfra", "is_container") is True
        assert reg.provider_flag("deepinfra", "skip_container_guards") is True
        assert reg.provider_flag("deepinfra", "cache_path_base", None) == "/workspace/.hermes"

    def test_strip_env_keys_union_includes_deepinfra_key(self):
        reg.register_provider(DeepInfraProvider())
        assert "DEEPINFRA_API_KEY" in reg.plugin_strip_env_keys()

    def test_not_rejected_as_builtin_name_collision(self):
        assert "deepinfra" not in reg.BUILTIN_BACKEND_NAMES
        reg.register_provider(DeepInfraProvider())  # must not raise
        assert reg.get_provider("deepinfra") is not None


class TestEntryPointName:
    def test_plugin_entry_point_key_is_not_bare_deepinfra(self):
        """hermes-agent already bundles two unrelated plugins literally named
        "deepinfra" (plugins/image_gen/deepinfra, plugins/video_gen/deepinfra).
        `hermes plugins enable <name>`'s exact-match resolution
        (hermes_cli/plugins_cmd.py:_resolve_plugin_key) takes the first plugin
        whose bare manifest name matches, without checking uniqueness -- so if
        this package's entry-point key were also bare "deepinfra", `hermes
        plugins enable deepinfra` would silently enable one of those bundled
        plugins instead of this one. The key must stay distinct (verified live
        against hermes-agent: "deepinfra-sandbox" resolves correctly)."""
        import importlib.metadata as md

        eps = [
            ep for ep in md.entry_points(group="hermes_agent.plugins")
            if ep.value == "deepinfra_hermes_sandbox"
        ]
        assert len(eps) == 1
        assert eps[0].name != "deepinfra"
