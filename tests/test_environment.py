"""Unit tests for the DeepInfra deep_sands cloud sandbox environment backend.

Requires hermes-agent's own packages (tools.*, agent.*) importable -- these
tests exercise a plugin that runs inside a hermes-agent process, so they run
against a Python environment that also has hermes-agent installed (e.g. a
checked-out hermes-agent with `pip install -e .`, or hermes-agent itself as
a dev dependency). See README.md's "Running tests" section.
"""

import types as _types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers to build a mock `deepinfra` SDK module
# ---------------------------------------------------------------------------

def _make_exec_result(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def _make_sandbox(sandbox_id="sb-123", state="running"):
    sb = MagicMock()
    sb.id = sandbox_id
    sb.state = state
    sb.exec.return_value = _make_exec_result()
    sb.fs = MagicMock()
    return sb


def _patch_deepinfra_imports(monkeypatch):
    """Patch the deepinfra SDK so environment.py can be imported without it.

    Exceptions are real subclasses (not MagicMocks) so isinstance/except
    checks in the code under test behave exactly like the real SDK.
    """
    deepinfra_mod = _types.ModuleType("deepinfra")

    class DeepInfraError(Exception):
        pass

    class APIConnectionError(DeepInfraError):
        pass

    class APITimeoutError(APIConnectionError):
        pass

    class MaxRetriesExceededError(APIConnectionError):
        pass

    class APIStatusError(DeepInfraError):
        def __init__(self, message="", *, status_code=0, response=None):
            super().__init__(message)
            self.status_code = status_code
            self.response = response

    class BadRequestError(APIStatusError):
        pass

    class AuthenticationError(APIStatusError):
        pass

    class PermissionDeniedError(APIStatusError):
        pass

    class NotFoundError(APIStatusError):
        pass

    class ConflictError(APIStatusError):
        pass

    class ContentTooLargeError(APIStatusError):
        pass

    class RateLimitError(APIStatusError):
        pass

    TooManySandboxesError = RateLimitError

    class CapacityError(APIStatusError):
        pass

    class InternalServerError(APIStatusError):
        pass

    class SandboxError(DeepInfraError):
        pass

    class SandboxWaitError(SandboxError):
        def __init__(self, message="", *, sandbox_id=None):
            super().__init__(message)
            self.sandbox_id = sandbox_id

    class SandboxTimeoutError(SandboxWaitError):
        pass

    class SandboxFailedError(SandboxWaitError):
        pass

    class SandboxExecError(SandboxError):
        pass

    class CommandFailedError(SandboxError):
        def __init__(self, result):
            super().__init__(f"exit {result.returncode}")
            self.result = result

    deepinfra_mod.Sandbox = MagicMock()
    deepinfra_mod.DeepInfraError = DeepInfraError
    deepinfra_mod.APIConnectionError = APIConnectionError
    deepinfra_mod.APITimeoutError = APITimeoutError
    deepinfra_mod.MaxRetriesExceededError = MaxRetriesExceededError
    deepinfra_mod.APIStatusError = APIStatusError
    deepinfra_mod.BadRequestError = BadRequestError
    deepinfra_mod.AuthenticationError = AuthenticationError
    deepinfra_mod.PermissionDeniedError = PermissionDeniedError
    deepinfra_mod.NotFoundError = NotFoundError
    deepinfra_mod.ConflictError = ConflictError
    deepinfra_mod.ContentTooLargeError = ContentTooLargeError
    deepinfra_mod.RateLimitError = RateLimitError
    deepinfra_mod.TooManySandboxesError = TooManySandboxesError
    deepinfra_mod.CapacityError = CapacityError
    deepinfra_mod.InternalServerError = InternalServerError
    deepinfra_mod.SandboxError = SandboxError
    deepinfra_mod.SandboxWaitError = SandboxWaitError
    deepinfra_mod.SandboxTimeoutError = SandboxTimeoutError
    deepinfra_mod.SandboxFailedError = SandboxFailedError
    deepinfra_mod.SandboxExecError = SandboxExecError
    deepinfra_mod.CommandFailedError = CommandFailedError

    monkeypatch.setitem(__import__("sys").modules, "deepinfra", deepinfra_mod)
    return deepinfra_mod


def _run_and_collect(handle, timeout=5):
    rc = handle.wait(timeout=timeout)
    data = handle.stdout.read()
    return data, rc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def deepinfra_sdk(monkeypatch):
    return _patch_deepinfra_imports(monkeypatch)


@pytest.fixture()
def make_env(deepinfra_sdk, monkeypatch):
    monkeypatch.setattr("tools.environments.base.is_interrupted", lambda: False)
    monkeypatch.setattr("tools.credential_files.get_credential_file_mounts", lambda: [])
    monkeypatch.setattr("tools.credential_files.get_skills_directory_mount", lambda **kw: None)
    monkeypatch.setattr("tools.credential_files.iter_skills_files", lambda **kw: [])
    monkeypatch.setattr("tools.credential_files.iter_cache_files", lambda **kw: [])

    def _factory(sandbox=None, **kwargs):
        sandbox = sandbox or _make_sandbox()
        deepinfra_sdk.Sandbox.create = MagicMock(return_value=sandbox)

        from deepinfra_hermes_sandbox.environment import DeepInfraEnvironment

        kwargs.setdefault("cwd", "/workspace")
        kwargs.setdefault("timeout", 60)
        env = DeepInfraEnvironment(**kwargs)
        env._mock_sandbox = sandbox
        return env

    return _factory


# ---------------------------------------------------------------------------
# Sandbox creation
# ---------------------------------------------------------------------------

class TestCreateSandbox:
    def test_create_passes_task_tag_and_injective_creation_id(self, make_env, deepinfra_sdk, monkeypatch):
        monkeypatch.delenv("DEEPINFRA_SANDBOX_PLAN", raising=False)
        make_env(task_id="mytask")
        _, kwargs = deepinfra_sdk.Sandbox.create.call_args
        assert kwargs["plan"] == ""
        assert kwargs["wait"] is True
        assert kwargs["tags"]["hermes_task_id"] == "mytask"
        creation_id = kwargs["tags"]["hermes_creation_id"]
        assert isinstance(creation_id, str) and len(creation_id) == 32  # uuid4().hex

    def test_create_reads_plan_from_env_var_when_set(self, make_env, deepinfra_sdk, monkeypatch):
        monkeypatch.setenv("DEEPINFRA_SANDBOX_PLAN", "large")
        make_env(task_id="mytask")
        _, kwargs = deepinfra_sdk.Sandbox.create.call_args
        assert kwargs["plan"] == "large"

    def test_create_strips_whitespace_from_plan_env_var(self, make_env, deepinfra_sdk, monkeypatch):
        monkeypatch.setenv("DEEPINFRA_SANDBOX_PLAN", "  large  ")
        make_env(task_id="mytask")
        _, kwargs = deepinfra_sdk.Sandbox.create.call_args
        assert kwargs["plan"] == "large"

    def test_two_creations_get_distinct_creation_ids(self, make_env, deepinfra_sdk):
        """The creation-id tag must be unique per attempt, not per task_id --
        it's what makes ambiguous-error reconciliation safe (see below)."""
        make_env(task_id="same-task")
        first_id = deepinfra_sdk.Sandbox.create.call_args.kwargs["tags"]["hermes_creation_id"]
        make_env(task_id="same-task")
        second_id = deepinfra_sdk.Sandbox.create.call_args.kwargs["tags"]["hermes_creation_id"]
        assert first_id != second_id

    def test_boot_failure_cleans_up_leaked_sandbox(self, deepinfra_sdk):
        """A SandboxWaitError with .sandbox_id must terminate the leaked
        sandbox before re-raising, so a failed boot never leaks a billable
        sandbox against the 5-per-account cap."""
        err = deepinfra_sdk.SandboxTimeoutError("boot timed out", sandbox_id="sb-failed")
        deepinfra_sdk.Sandbox.create = MagicMock(side_effect=err)
        from_id_handle = MagicMock()
        deepinfra_sdk.Sandbox.from_id = MagicMock(return_value=from_id_handle)

        from deepinfra_hermes_sandbox.environment import DeepInfraEnvironment

        with pytest.raises(deepinfra_sdk.SandboxTimeoutError):
            DeepInfraEnvironment(cwd="/workspace", timeout=60)

        deepinfra_sdk.Sandbox.from_id.assert_called_once_with("sb-failed")
        from_id_handle.terminate.assert_called_once()

    def test_boot_failure_without_sandbox_id_does_not_call_from_id(self, deepinfra_sdk):
        err = deepinfra_sdk.SandboxFailedError("no id available", sandbox_id=None)
        deepinfra_sdk.Sandbox.create = MagicMock(side_effect=err)
        deepinfra_sdk.Sandbox.from_id = MagicMock()

        from deepinfra_hermes_sandbox.environment import DeepInfraEnvironment

        with pytest.raises(deepinfra_sdk.SandboxFailedError):
            DeepInfraEnvironment(cwd="/workspace", timeout=60)

        deepinfra_sdk.Sandbox.from_id.assert_not_called()

    def test_api_error_during_wait_poll_falls_back_to_creation_id_lookup(self, deepinfra_sdk):
        """A raw APIStatusError/APIConnectionError during the wait-poll (the
        create() POST already succeeded -- a real sandbox exists -- but a
        later refresh() call fails) carries no .sandbox_id the way
        SandboxWaitError does. Recovery falls back to Sandbox.list() by the
        injective hermes_creation_id tag (NOT hermes_task_id -- see the
        ownership-safety test below for why that distinction matters), so
        the leaked sandbox still gets cleaned up."""
        deepinfra_sdk.Sandbox.create = MagicMock(
            side_effect=deepinfra_sdk.APIConnectionError("connection dropped")
        )
        leaked = MagicMock()
        leaked.id = "sb-leaked"
        deepinfra_sdk.Sandbox.list = MagicMock(return_value=[leaked])
        from_id_handle = MagicMock()
        deepinfra_sdk.Sandbox.from_id = MagicMock(return_value=from_id_handle)

        from deepinfra_hermes_sandbox.environment import DeepInfraEnvironment

        with pytest.raises(deepinfra_sdk.APIConnectionError):
            DeepInfraEnvironment(cwd="/workspace", timeout=60, task_id="leaktask")

        list_kwargs = deepinfra_sdk.Sandbox.list.call_args.kwargs
        assert set(list_kwargs["tags"].keys()) == {"hermes_creation_id"}
        deepinfra_sdk.Sandbox.from_id.assert_called_once_with("sb-leaked")
        from_id_handle.terminate.assert_called_once()

    def test_ambiguous_create_error_never_terminates_a_foreign_sandbox(self, deepinfra_sdk):
        """Regression for the ownership bug: task_id collapses to a shared
        value across independent processes/sessions by design (hermes-agent's
        own _resolve_container_task_id() collapses nearly every task_id to
        "default" without a session context or isolation override -- true of
        every backend, not deepinfra-specific). Two unrelated hermes-agent
        processes sharing one DeepInfra account can end up with sandboxes
        both tagged hermes_task_id=default. If reconciliation looked up by
        that tag alone, a connection error during MY wait-poll could destroy
        a HEALTHY, UNRELATED sandbox that happens to share the task label.

        This seeds exactly that scenario -- Sandbox.list() is asked for the
        injective creation-id tag, not the shared task tag, so it must be
        called in a way that could never match a foreign sandbox regardless
        of what Sandbox.list() is mocked to return.
        """
        deepinfra_sdk.Sandbox.create = MagicMock(
            side_effect=deepinfra_sdk.APIConnectionError("connection dropped")
        )
        foreign_healthy_sandbox = MagicMock()
        foreign_healthy_sandbox.id = "sb-foreign-healthy"

        def _list(*, tags):
            # A real deep_sands account-scoped Sandbox.list(tags=...) filters
            # server-side/client-side by exact tag match. Simulate that
            # faithfully: only return the foreign sandbox if queried by the
            # shared task tag (which it also happens to carry) -- prove the
            # code never does that query in the first place.
            if "hermes_task_id" in tags:
                return [foreign_healthy_sandbox]
            return []  # nothing tagged with this call's unique creation id

        deepinfra_sdk.Sandbox.list = MagicMock(side_effect=_list)
        deepinfra_sdk.Sandbox.from_id = MagicMock()

        from deepinfra_hermes_sandbox.environment import DeepInfraEnvironment

        with pytest.raises(deepinfra_sdk.APIConnectionError):
            DeepInfraEnvironment(cwd="/workspace", timeout=60, task_id="default")

        # The only Sandbox.list() call must have been keyed by the unique
        # creation id -- never by the shared task tag -- so the foreign
        # sandbox is structurally unreachable, not just "didn't happen to
        # match" this one mock configuration.
        for call in deepinfra_sdk.Sandbox.list.call_args_list:
            assert set(call.kwargs["tags"].keys()) == {"hermes_creation_id"}
        deepinfra_sdk.Sandbox.from_id.assert_not_called()
        foreign_healthy_sandbox.terminate.assert_not_called()


# ---------------------------------------------------------------------------
# Per-turn persistence flag
# ---------------------------------------------------------------------------

class TestPersistenceFlag:
    def test_defaults_to_persistent(self, make_env):
        """Regression: without self._persistent set, terminal_tool's
        is_persistent_env() always returns False for this backend, so
        cleanup_task_resources() tears the sandbox down and rebuilds it from
        scratch after every single agent turn instead of only at real
        session end / idle reap."""
        env = make_env()
        assert env._persistent is True

    def test_honors_persistent_filesystem_false(self, make_env):
        env = make_env(persistent_filesystem=False)
        assert env._persistent is False


# ---------------------------------------------------------------------------
# Default cwd
# ---------------------------------------------------------------------------

class TestDefaultCwd:
    def test_generic_root_default_becomes_workspace(self, make_env):
        """hermes-agent core's default-cwd resolution has no deepinfra-
        specific branch and falls through to the generic container default,
        "/root" -- which is wiped on every deep_sands stop/start cycle
        (only /workspace persists). The plugin substitutes its own default
        rather than requiring a hermes-agent core change."""
        env = make_env(cwd="/root")
        assert env.cwd == "/workspace"

    def test_explicit_cwd_is_respected(self, make_env):
        env = make_env(cwd="/workspace/myproject")
        assert env.cwd == "/workspace/myproject"

    def test_execute_rewrites_per_call_root_cwd_too(self, make_env, monkeypatch):
        """Regression: terminal_tool.py resolves and passes the generic
        "/root" default EXPLICITLY on every execute() call (not just at
        construction), and BaseEnvironment's `effective_cwd = cwd or
        self.cwd` means that per-call value always wins over whatever
        __init__ normalized self.cwd to. Confirmed empirically against the
        live API: without this override, every command silently ran in
        /root regardless of the constructor-level fix. execute() must
        rewrite an incoming "/root"/empty cwd on every call, not just once."""
        env = make_env()
        captured = {}

        def _fake_base_execute(self, command, cwd="", **kwargs):
            captured["cwd"] = cwd
            return {"output": "", "returncode": 0}

        import tools.environments.base as base_mod
        monkeypatch.setattr(base_mod.BaseEnvironment, "execute", _fake_base_execute)

        env.execute("pwd", cwd="/root")
        assert captured["cwd"] == "/workspace"

        env.execute("pwd", cwd="")
        assert captured["cwd"] == "/workspace"

        env.execute("pwd", cwd="/workspace/myproject")
        assert captured["cwd"] == "/workspace/myproject"


# ---------------------------------------------------------------------------
# Idle-resume (_before_execute)
# ---------------------------------------------------------------------------

class TestBeforeExecute:
    def test_restarts_stopped_sandbox(self, make_env):
        env = make_env()
        env._sandbox.state = "stopped"
        env._before_execute()
        env._sandbox.start.assert_called_once()

    def test_no_restart_when_running(self, make_env):
        env = make_env()
        env._sandbox.state = "running"
        env._before_execute()
        env._sandbox.start.assert_not_called()

    def test_refresh_failure_does_not_raise(self, make_env):
        env = make_env()
        env._sandbox.refresh.side_effect = RuntimeError("network blip")
        env._before_execute()  # must not raise


# ---------------------------------------------------------------------------
# Execute happy path + argv shape
# ---------------------------------------------------------------------------

class TestExecute:
    def test_basic_command(self, make_env):
        sb = _make_sandbox()
        sb.exec.side_effect = [
            _make_exec_result(returncode=0),                    # init_session bootstrap
            _make_exec_result(stdout="hello\n", returncode=0),  # actual command
        ]
        env = make_env(sandbox=sb)

        result = env.execute("echo hello")
        assert "hello" in result["output"]
        assert result["returncode"] == 0

    def test_cmd_string_reaches_exec_unquoted(self, make_env):
        """Regression: cmd_string must reach sb.exec() as the verbatim last
        *args element, not shlex.quote-wrapped -- the server applies its own
        quoting per argv element, so client-side quoting on top would
        double-quote and corrupt anything with literal quotes/newlines."""
        env = make_env()
        env._mock_sandbox.exec.reset_mock()
        env._mock_sandbox.exec.return_value = _make_exec_result()

        cmd = "echo 'hi'; rm -rf /tmp/x"
        handle = env._run_bash(cmd, login=False, timeout=30)
        _run_and_collect(handle)

        args, kwargs = env._mock_sandbox.exec.call_args
        assert args == ("bash", "-c", cmd)
        assert kwargs["timeout"] == 30

    def test_login_shell_argv(self, make_env):
        env = make_env()
        env._mock_sandbox.exec.reset_mock()
        env._mock_sandbox.exec.return_value = _make_exec_result()

        handle = env._run_bash("whoami", login=True, timeout=10)
        _run_and_collect(handle)

        args, _ = env._mock_sandbox.exec.call_args
        assert args == ("bash", "-l", "-c", "whoami")


# ---------------------------------------------------------------------------
# Exec error mapping
# ---------------------------------------------------------------------------

class TestExecErrorMapping:
    def test_exec_error_maps_to_exit_124(self, make_env, deepinfra_sdk):
        env = make_env()
        env._mock_sandbox.exec.side_effect = deepinfra_sdk.SandboxExecError("timed out server-side")

        handle = env._run_bash("sleep 999", login=False, timeout=5)
        output, rc = _run_and_collect(handle)

        assert rc == 124
        assert "timed out server-side" in output

    def test_conflict_error_maps_to_exit_1_with_message(self, make_env, deepinfra_sdk):
        env = make_env()
        env._mock_sandbox.exec.side_effect = deepinfra_sdk.ConflictError("wrong state")

        handle = env._run_bash("echo hi", login=False, timeout=5)
        output, rc = _run_and_collect(handle)

        assert rc == 1
        assert "wrong state" in output

    def test_connection_error_maps_to_exit_1_with_message(self, make_env, deepinfra_sdk):
        env = make_env()
        env._mock_sandbox.exec.side_effect = deepinfra_sdk.APIConnectionError("socket dropped")

        handle = env._run_bash("echo hi", login=False, timeout=5)
        output, rc = _run_and_collect(handle)

        assert rc == 1
        assert "socket dropped" in output


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

class TestCancel:
    def test_kill_calls_blocking_stop(self, make_env):
        """Regression: a fire-and-forget stop(wait=False) would return while
        the sandbox is still mid-"stopping", and _before_execute()'s exact
        state=="stopped" check would then skip restarting it for the very
        next command -- blocking stop() (the SDK default) avoids the race
        entirely, matching Daytona's own cancel_fn."""
        env = make_env()
        env._mock_sandbox.exec.return_value = _make_exec_result()

        handle = env._run_bash("sleep 5", login=False, timeout=30)
        handle.kill()

        env._mock_sandbox.stop.assert_called_once_with()

    def test_kill_swallows_errors(self, make_env):
        env = make_env()
        env._mock_sandbox.stop.side_effect = RuntimeError("stop failed")

        handle = env._run_bash("sleep 5", login=False, timeout=30)
        handle.kill()  # must not raise


# ---------------------------------------------------------------------------
# File sync transport
# ---------------------------------------------------------------------------

class TestUpload:
    def test_single_upload_never_issues_mkdir(self, make_env, tmp_path):
        """Regression: server-side write auto-creates parent dirs -- _upload
        must never issue a separate mkdir exec call."""
        env = make_env()
        env._mock_sandbox.exec.reset_mock()
        env._mock_sandbox.fs.reset_mock()

        host_file = tmp_path / "token.txt"
        host_file.write_text("secret", encoding="utf-8")

        env._upload(str(host_file), "/workspace/.hermes/token.txt")

        env._mock_sandbox.exec.assert_not_called()
        env._mock_sandbox.fs.write.assert_called_once_with(
            "/workspace/.hermes/token.txt", b"secret"
        )


class TestBulkUpload:
    def test_bulk_upload_call_sequence(self, make_env, tmp_path):
        env = make_env()
        env._mock_sandbox.exec.reset_mock()
        env._mock_sandbox.fs.reset_mock()

        f1 = tmp_path / "a.txt"
        f1.write_text("aaa", encoding="utf-8")
        f2 = tmp_path / "b.txt"
        f2.write_text("bbb", encoding="utf-8")

        env._bulk_upload([
            (str(f1), "/workspace/.hermes/a.txt"),
            (str(f2), "/workspace/.hermes/b.txt"),
        ])

        assert env._mock_sandbox.fs.write.call_count == 1
        tar_path, tar_bytes = env._mock_sandbox.fs.write.call_args[0]
        assert tar_path.startswith("/workspace/.hermes_sync/")
        assert tar_path.endswith(".tar")

        assert env._mock_sandbox.exec.call_count == 2
        extract_args = env._mock_sandbox.exec.call_args_list[0][0]
        assert extract_args == ("tar", "xf", tar_path, "-C", "/")
        cleanup_args = env._mock_sandbox.exec.call_args_list[1][0]
        assert cleanup_args == ("rm", "-f", tar_path)

    def test_empty_file_list_is_noop(self, make_env):
        env = make_env()
        env._mock_sandbox.exec.reset_mock()
        env._mock_sandbox.fs.reset_mock()

        env._bulk_upload([])

        env._mock_sandbox.exec.assert_not_called()
        env._mock_sandbox.fs.write.assert_not_called()

    def test_oversized_tar_falls_back_to_per_file_upload(self, make_env, tmp_path, monkeypatch):
        monkeypatch.setattr("deepinfra_hermes_sandbox.environment._BULK_TAR_MAX_BYTES", 1)
        env = make_env()
        env._mock_sandbox.exec.reset_mock()
        env._mock_sandbox.fs.reset_mock()

        f1 = tmp_path / "a.txt"
        f1.write_text("aaa", encoding="utf-8")
        f2 = tmp_path / "b.txt"
        f2.write_text("bbb", encoding="utf-8")

        env._bulk_upload([
            (str(f1), "/workspace/.hermes/a.txt"),
            (str(f2), "/workspace/.hermes/b.txt"),
        ])

        assert env._mock_sandbox.fs.write.call_count == 2
        env._mock_sandbox.exec.assert_not_called()
        written_paths = {c[0][0] for c in env._mock_sandbox.fs.write.call_args_list}
        assert written_paths == {"/workspace/.hermes/a.txt", "/workspace/.hermes/b.txt"}


class TestBulkDownload:
    def test_bulk_download_tar_round_trip(self, make_env, tmp_path):
        env = make_env()
        env._mock_sandbox.exec.reset_mock()
        env._mock_sandbox.fs.reset_mock()
        env._mock_sandbox.exec.return_value = _make_exec_result(returncode=0)
        env._mock_sandbox.fs.read.return_value = b"fake-tar-bytes"

        dest = tmp_path / "pulled.tar"
        env._bulk_download(dest)

        assert env._mock_sandbox.exec.call_count == 3
        mkdir_args = env._mock_sandbox.exec.call_args_list[0][0]
        assert mkdir_args[:2] == ("mkdir", "-p")
        create_args = env._mock_sandbox.exec.call_args_list[1][0]
        assert create_args[:2] == ("tar", "cf")
        assert create_args[3:] == ("-C", "/", "workspace/.hermes")
        cleanup_args = env._mock_sandbox.exec.call_args_list[2][0]
        assert cleanup_args[0] == "rm"

        read_path = env._mock_sandbox.fs.read.call_args[0][0]
        assert read_path == create_args[2]
        assert dest.read_bytes() == b"fake-tar-bytes"

    def test_bulk_download_raises_on_tar_create_failure(self, make_env, tmp_path):
        """Regression: a failed tar-create must not silently proceed to read
        back a file that was never written."""
        env = make_env()
        env._mock_sandbox.exec.reset_mock()
        env._mock_sandbox.fs.reset_mock()

        def _exec_side_effect(*args, **kwargs):
            if args[:2] == ("tar", "cf"):
                return _make_exec_result(stderr="No such file or directory", returncode=2)
            return _make_exec_result(returncode=0)

        env._mock_sandbox.exec.side_effect = _exec_side_effect

        with pytest.raises(RuntimeError):
            env._bulk_download(tmp_path / "pulled.tar")

        env._mock_sandbox.fs.read.assert_not_called()


class TestDelete:
    def test_delete_uses_argv_not_shell_string(self, make_env):
        """deep_sands exec() takes real argv -- each path is its own
        element, no shlex.quote/shell-string assembly needed."""
        env = make_env()
        env._mock_sandbox.exec.reset_mock()

        env._delete(["/workspace/.hermes/a.txt", "/workspace/.hermes/evil; rm -rf /"])

        env._mock_sandbox.exec.assert_called_once_with(
            "rm", "-f",
            "/workspace/.hermes/a.txt",
            "/workspace/.hermes/evil; rm -rf /",
        )

    def test_empty_list_is_noop(self, make_env):
        env = make_env()
        env._mock_sandbox.exec.reset_mock()
        env._delete([])
        env._mock_sandbox.exec.assert_not_called()


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

class TestCleanup:
    def test_cleanup_terminates_and_drops_handle_on_success(self, make_env):
        env = make_env()
        env.cleanup()
        env._mock_sandbox.terminate.assert_called_once()
        assert env._sandbox is None

    def test_cleanup_preserves_handle_on_terminate_failure_then_retries_successfully(self, make_env):
        """Regression for the second ownership bug: cleanup() used to drop
        self._sandbox unconditionally even when terminate() failed. That
        loses the ONLY reference needed to retry -- an indeterminate
        terminate() failure is not confirmation the (billable) sandbox is
        gone. This proves the handle survives a failed attempt and a
        second cleanup() call can still reconcile/terminate the exact same
        sandbox."""
        env = make_env()
        env._mock_sandbox.terminate.side_effect = [RuntimeError("transient failure"), None]

        env.cleanup()  # first attempt: terminate() fails
        assert env._sandbox is not None, (
            "handle must survive an indeterminate terminate() failure"
        )

        env.cleanup()  # second attempt: same exact sandbox, terminate() succeeds
        assert env._mock_sandbox.terminate.call_count == 2
        assert env._sandbox is None

    def test_cleanup_is_idempotent_after_confirmed_success(self, make_env):
        env = make_env()
        env.cleanup()
        env._mock_sandbox.terminate.reset_mock()
        env.cleanup()  # second call: sandbox already None, must no-op
        env._mock_sandbox.terminate.assert_not_called()
