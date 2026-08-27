"""DeepInfra cloud execution environment.

Uses the official `deepinfra` Python SDK to run commands in deep_sands cloud
sandboxes (isolated microVMs, server-side Kubernetes pod-exec -- no SSH, no
network access to the sandbox at all). Ephemeral only: every session gets a
fresh sandbox, terminated on cleanup. Persistent/resumable sandboxes (to
match Daytona's default behavior) are a tracked fast-follow, not v1 scope.

Runs as a hermes-agent plugin (see __init__.py's DeepInfraProvider) -- these
imports assume execution inside a running hermes-agent process, the same way
every other TerminalEnvironmentProvider plugin does.
"""

import io
import logging
import os
import tarfile
import threading
import uuid
from pathlib import Path

from tools.environments.base import (
    BaseEnvironment,
    _ThreadedProcessHandle,
)
from tools.environments.file_sync import (
    FileSyncManager,
    iter_sync_files,
)

logger = logging.getLogger(__name__)

# Must be under /workspace -- deep_sands' fs endpoints reject any other path
# with 400 ERR_PATH_OUTSIDE_WORKSPACE.
CACHE_PATH_BASE = "/workspace/.hermes"

# Headroom under the 100 MiB fs/content cap. The SDK has no batch/dir upload
# yet (fs.upload_dir() is roadmap-only), so bulk sync is a self-built
# tar-and-exec workaround -- falls back to per-file upload above this size.
_BULK_TAR_MAX_BYTES = 80 * 1024 * 1024

_SYNC_DIR = f"{CACHE_PATH_BASE}_sync"

# Deliberately NOT auto-selecting a deep_sands plan tier from
# container_cpu/container_memory. The hermes-agent core defaults both
# (1 core / 5120 MB) for EVERY backend even when the user never set them --
# there is no way at this layer to distinguish "the user explicitly wants
# 5GB" from "this is just the generic cross-backend default". Verified
# against the real catalog: the platform's own default plan ("medium") has
# only 4GB RAM, so naively fitting the 5GB shared default into the smallest
# satisfying tier silently picks "large" (2x medium's hourly price) for
# effectively every user who never customized sizing at all.
#
# DEEPINFRA_SANDBOX_PLAN is the escape hatch: unlike container_cpu/memory,
# nothing else in hermes-agent could ever set this env var by accident, so
# its presence really does mean "the user wants this plan" -- no ambiguity,
# no auto-fitting logic, no footgun. Absent or empty -> deep_sands' own
# default plan (currently "medium"). An invalid value is rejected by the
# server itself (a clear error surfaces from Sandbox.create() the same way
# any other misconfiguration would); no client-side validation needed.
# Longer-term this belongs in a provider-owned config key instead of a raw
# env var (hermes-agent issue #96161 tracks exactly that: provider-scoped
# `terminal.backends.<provider>` config, which would let plan selection
# compose properly with the rest of terminal config) -- revisit once that
# lands.
_PLAN_ENV_VAR = "DEEPINFRA_SANDBOX_PLAN"


class DeepInfraEnvironment(BaseEnvironment):
    """DeepInfra deep_sands cloud sandbox execution backend.

    Spawn-per-call via _ThreadedProcessHandle wrapping blocking SDK calls,
    same shape as Daytona/Modal. cancel_fn wired to sandbox.stop() (blocking,
    the SDK default) for interrupt support -- deep_sands has no per-command
    cancel, so stopping the whole sandbox is the only real lever, and a
    fire-and-forget stop(wait=False) would race _before_execute()'s exact
    state=="stopped" restart check on the very next command.
    """

    _stdin_mode = "heredoc"  # deep_sands exec has no stdin field

    def __init__(
        self,
        *,
        cwd: str = "/workspace",
        timeout: int = 60,
        task_id: str = "default",
        persistent_filesystem: bool = True,
        **_unused,
    ):
        # deep_sands only persists /workspace across a stop/start cycle;
        # a caller passing the generic container default ("/root") really
        # means "no cwd preference", so substitute our own default rather
        # than requiring a hermes-agent core change to special-case this
        # backend's default cwd (the plugin owns this decision entirely).
        # This normalizes construction time, but terminal_tool.py resolves
        # and passes an explicit `cwd` on every subsequent execute() call
        # too (not just at construction) -- see the execute() override
        # below for why this alone isn't sufficient.
        if cwd in ("", "/root", None):
            cwd = "/workspace"
        super().__init__(cwd=cwd, timeout=timeout)

        from deepinfra import Sandbox

        self._Sandbox = Sandbox
        self._task_id = task_id
        # Gates per-turn teardown (terminal_tool.is_persistent_env()) -- NOT
        # cross-session resume (v1 has none: _create_sandbox always creates
        # fresh). Without this, cleanup_task_resources() tears the sandbox
        # down and rebuilds it from scratch after every single agent turn.
        # cleanup() below always terminates regardless of this flag -- there
        # is no resume-by-tag lookup yet, so a stopped-not-deleted sandbox
        # would just leak with nothing to ever find it again.
        self._persistent = persistent_filesystem
        self._lock = threading.Lock()
        self._sandbox = self._create_sandbox(Sandbox, task_id)
        logger.info("DeepInfra: created sandbox %s for task %s", self._sandbox.id, task_id)

        self._sync_manager = FileSyncManager(
            get_files_fn=lambda: iter_sync_files(CACHE_PATH_BASE),
            upload_fn=self._upload,
            delete_fn=self._delete,
            bulk_upload_fn=self._bulk_upload,
            bulk_download_fn=self._bulk_download,
        )
        self._sync_manager.sync(force=True)
        self.init_session()

    # ------------------------------------------------------------------
    # Sandbox lifecycle
    # ------------------------------------------------------------------

    def _create_sandbox(self, Sandbox, task_id: str):
        """Create and wait for a running sandbox.

        On boot failure the SDK's own SandboxWaitError carries the
        sandbox_id specifically so a failed-boot sandbox can be found and
        cleaned up (see the SDK's own SandboxWaitError docstring) -- without
        this a failed boot leaks a billable sandbox against the
        5-per-account cap.

        A raw APIStatusError/APIConnectionError during the wait-poll (e.g.
        refresh() itself rate-limited or dropped mid-wait) doesn't carry a
        sandbox_id the same way -- the create() POST already succeeded by
        that point, so a sandbox genuinely exists and needs cleanup, but we
        have no direct handle to it.

        The fallback MUST NOT reconcile by ``hermes_task_id`` alone: that
        tag is not an ownership token. terminal_tool._resolve_container_
        task_id() collapses nearly every task_id to a shared "default"
        value unless a session context or isolation override is registered
        (this plugin's own live integration tests document exactly that
        collapsing behavior), and independent hermes-agent processes can
        share one DeepInfra account/key -- so a task-id-only lookup could
        match and terminate a HEALTHY, UNRELATED sandbox belonging to a
        different process (confirmed possible: two processes both landing
        on hermes_task_id=default is the common case, not an edge case).
        Instead, every creation attempt gets its own fresh, injective
        creation-id tag; the fallback only ever reconciles by that exact
        value, so a match is provably this call's own sandbox and nothing
        else's.
        """
        from deepinfra import APIConnectionError, APIStatusError, SandboxWaitError

        creation_id = uuid.uuid4().hex
        tags = {"hermes_task_id": task_id, "hermes_creation_id": creation_id}
        plan = os.getenv(_PLAN_ENV_VAR, "").strip()
        try:
            return Sandbox.create(plan=plan, tags=tags, wait=True)
        except SandboxWaitError as e:
            self._terminate_leaked_sandbox(Sandbox, getattr(e, "sandbox_id", None))
            raise
        except (APIStatusError, APIConnectionError):
            try:
                for sb in Sandbox.list(tags={"hermes_creation_id": creation_id}):
                    self._terminate_leaked_sandbox(Sandbox, sb.id)
            except Exception:
                pass
            raise

    @staticmethod
    def _terminate_leaked_sandbox(Sandbox, sandbox_id) -> None:
        if not sandbox_id:
            return
        try:
            Sandbox.from_id(sandbox_id).terminate()
        except Exception:
            logger.warning("DeepInfra: failed to clean up failed-boot sandbox %s", sandbox_id)

    def execute(self, command: str, cwd: str = "", **kwargs) -> dict:
        """Override to keep the /workspace cwd default in effect on every call.

        hermes-agent core has no deepinfra-specific branch in its
        default-cwd resolution (this backend ships as a plugin, not a
        built-in), so terminal_tool.py resolves and passes the generic
        container default ("/root") EXPLICITLY on every execute() call, not
        just at construction -- BaseEnvironment.execute()'s own
        `effective_cwd = cwd or self.cwd` means that per-call value always
        wins over whatever __init__ normalized self.cwd to. /root is wiped
        on every deep_sands stop/start cycle; only /workspace persists.
        Falling back to self.cwd (not a hardcoded "/workspace" again)
        preserves any `cd` the agent has done mid-session, since
        BaseEnvironment keeps self.cwd current via the cwd-marker
        extraction after each command.
        """
        if cwd in ("", "/root"):
            cwd = self.cwd
        return super().execute(command, cwd, **kwargs)

    def _before_execute(self) -> None:
        """Restart sandbox if it was stopped (idle-timeout auto-stop), then sync files."""
        with self._lock:
            try:
                self._sandbox.refresh()
                if self._sandbox.state == "stopped":
                    self._sandbox.start()
                    logger.info("DeepInfra: restarted sandbox %s", self._sandbox.id)
            except Exception as e:
                logger.warning(
                    "DeepInfra: failed to refresh/restart sandbox %s: %s",
                    self._sandbox.id, e,
                )
        self._sync_manager.sync()

    def _run_bash(self, cmd_string: str, *, login: bool = False,
                  timeout: int = 120,
                  stdin_data: str | None = None):
        """Return a _ThreadedProcessHandle wrapping a blocking deep_sands exec call."""
        sandbox = self._sandbox
        lock = self._lock

        def cancel():
            with lock:
                try:
                    # Blocking (SDK default), matching Daytona's own cancel_fn:
                    # a fire-and-forget stop(wait=False) would return while
                    # the sandbox is still mid-"stopping", and
                    # _before_execute()'s exact state=="stopped" check would
                    # then skip restarting it for the very next command.
                    sandbox.stop()
                except Exception:
                    pass

        if login:
            argv = ("bash", "-l", "-c", cmd_string)
        else:
            argv = ("bash", "-c", cmd_string)

        def exec_fn() -> tuple[str, int]:
            from deepinfra import APIConnectionError, APIStatusError, SandboxExecError

            try:
                r = sandbox.exec(*argv, timeout=timeout)
            except SandboxExecError as e:
                # exec-level timeout/stream failure -- distinct from
                # SandboxTimeoutError, which is create()/start()/stop()'s own
                # wait-until-state polling and never fires here.
                return (f"[DeepInfra: {e}]", 124)
            except (APIStatusError, APIConnectionError) as e:
                # Infra fault (conflict, capacity, rate limit, connection
                # drop, ...), not a command failure -- surface the real
                # message rather than letting it disappear into a bare
                # "exit 1, no output" (the threaded exec adapter has no
                # channel back to the caller for a raised exception).
                return (f"[DeepInfra sandbox error: {e}]", 1)
            return (r.stdout + r.stderr, r.returncode)

        return _ThreadedProcessHandle(exec_fn, cancel_fn=cancel)

    def cleanup(self):
        with self._lock:
            if self._sandbox is None:
                return

            if self._sync_manager:
                logger.info("DeepInfra: syncing files from sandbox...")
                try:
                    self._sync_manager.sync_back()
                except Exception as e:
                    logger.warning("DeepInfra: sync_back failed: %s", e)

            try:
                self._sandbox.terminate()
                logger.info("DeepInfra: terminated sandbox %s", self._sandbox.id)
            except Exception as e:
                # Do NOT drop the handle here: terminate() failing is
                # indeterminate, not confirmation the sandbox is gone. If we
                # null self._sandbox unconditionally, a transient failure
                # permanently loses the only reference needed to retry --
                # the sandbox may still exist and still be billing. Leaving
                # self._sandbox set means a subsequent cleanup() call (the
                # idle reaper retries, or the caller retries explicitly)
                # naturally retries the exact same terminate() against the
                # exact same sandbox, not a tag-based re-lookup.
                logger.warning("DeepInfra: cleanup failed, will retry on next call: %s", e)
                return
            self._sandbox = None

    # ------------------------------------------------------------------
    # File sync transport
    # ------------------------------------------------------------------

    def _upload(self, host_path: str, remote_path: str) -> None:
        """Upload a single file. No mkdir needed -- server-side write
        auto-creates parent dirs (it wraps the bytes in a tar and runs
        `tar xf - -C /` in the pod)."""
        self._sandbox.fs.write(remote_path, Path(host_path).read_bytes())

    def _bulk_upload(self, files: list[tuple[str, str]]) -> None:
        """Upload many files as one tar, extracted server-side via exec.

        deep_sands has no native batch-upload endpoint (unlike Daytona's
        fs.upload_files()), so this builds the tar client-side and pushes it
        through the same single-file fs.write() + exec("tar","xf",...) that
        Daytona's own bulk *download* uses -- just applied to the upload
        direction too, since both hit the identical "no batch transfer API"
        gap.
        """
        if not files:
            return

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for host_path, remote_path in files:
                tf.add(host_path, arcname=remote_path.lstrip("/"))
        data = buf.getvalue()

        if len(data) > _BULK_TAR_MAX_BYTES:
            for host_path, remote_path in files:
                self._upload(host_path, remote_path)
            return

        remote_tar = f"{_SYNC_DIR}/{uuid.uuid4().hex}.tar"
        self._sandbox.fs.write(remote_tar, data)
        self._sandbox.exec("tar", "xf", remote_tar, "-C", "/")
        try:
            self._sandbox.exec("rm", "-f", remote_tar)
        except Exception:
            pass  # best-effort cleanup

    def _bulk_download(self, dest: Path) -> None:
        """Download remote CACHE_PATH_BASE as a tar archive.

        Unlike _bulk_upload, whose fs.write() auto-creates _SYNC_DIR
        server-side (deep_sands' fs endpoints tar-extract into place), a
        plain `tar cf` shell command does NOT create its own output
        directory -- explicit mkdir needed, and unlike fs writes there's no
        server-side validation to catch a failure, so the exec's exit code
        must be checked directly or a failed tar silently leaves nothing to
        read back.
        """
        rel_base = CACHE_PATH_BASE.lstrip("/")
        remote_tar = f"{_SYNC_DIR}/{uuid.uuid4().hex}.tar"
        self._sandbox.exec("mkdir", "-p", _SYNC_DIR)
        r = self._sandbox.exec("tar", "cf", remote_tar, "-C", "/", rel_base)
        if r.returncode != 0:
            raise RuntimeError(
                f"DeepInfra: tar create failed (exit {r.returncode}): {r.stderr}"
            )
        data = self._sandbox.fs.read(remote_tar)
        dest.write_bytes(data)
        try:
            self._sandbox.exec("rm", "-f", remote_tar)
        except Exception:
            pass  # best-effort cleanup

    def _delete(self, remote_paths: list[str]) -> None:
        """Batch-delete remote files. deep_sands exec() takes real argv, so
        each path is its own element -- no shell-string quoting needed
        (unlike Daytona, whose exec() takes a shell string)."""
        if not remote_paths:
            return
        self._sandbox.exec("rm", "-f", *remote_paths)
