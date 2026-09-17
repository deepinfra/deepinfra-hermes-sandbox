"""DeepInfra cloud execution environment.

Uses the official `deepinfra` Python SDK to run commands in deep_sands cloud
sandboxes (isolated microVMs, server-side Kubernetes pod-exec -- no SSH, no
network access to the sandbox at all). When persistent, an idle-reaped
sandbox is stopped (not terminated) and resumed by task-tag lookup on the
next construction, mirroring Daytona's own resume-by-name pattern -- see
_try_resume_sandbox(). Cross-SESSION resume (a brand new session picking up
a previous one's sandbox) is still not implemented; this only covers the
idle-reap-mid-session case.

Runs as a hermes-agent plugin (see __init__.py's DeepInfraProvider) -- these
imports assume execution inside a running hermes-agent process, the same way
every other TerminalEnvironmentProvider plugin does.
"""

import io
import logging
import os
import shlex
import tarfile
import threading
import uuid
from pathlib import Path

from tools.environments.base import BaseEnvironment
# _ThreadedProcessHandle moved here in hermes-agent's base.py/base_output.py
# split (2026-09-02, 3bbec90f23) -- base.py no longer re-exports it. Import
# straight from base_output, matching how Daytona/Modal do it post-refactor.
from tools.environments.base_output import _ThreadedProcessHandle
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
        # Gates per-turn teardown (terminal_tool.is_persistent_env()) AND,
        # via _try_resume_sandbox()/cleanup() below, whether an idle-reaped
        # sandbox is stopped-and-resumable or fully terminated. Without this,
        # cleanup_task_resources() tears the sandbox down and rebuilds it
        # from scratch after every single agent turn.
        self._persistent = persistent_filesystem
        self._lock = threading.Lock()

        self._sandbox = self._try_resume_sandbox(Sandbox, task_id) if self._persistent else None
        if self._sandbox is None:
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

        This is a fresh, unconditional create -- called either directly (no
        persistence requested) or as the fallback when
        _try_resume_sandbox() found nothing safe to resume.
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

    @staticmethod
    def _try_resume_sandbox(Sandbox, task_id: str):
        """Resume a previously stopped, still-persistent sandbox for this
        task, or return None to fall back to a fresh create().

        Mirrors Daytona's own resume-by-name pattern (tools/environments/
        daytona.py: ``self._daytona.get(f"hermes-{task_id}")``) using what
        deep_sands actually exposes -- tag list + from_id, not a name
        lookup. This closes the idle-reap data-loss gap: hermes-agent's
        idle reaper (terminal.lifetime_seconds, default 300s) tears down
        and recreates the environment object after 5 minutes of inactivity;
        without a resume path, cleanup() had to fully terminate (or leak a
        stopped sandbox nothing would ever find again).

        Only ever resumes on an UNAMBIGUOUS single match in "stopped"
        state:
        - hermes_task_id is not an ownership token -- it collapses to a
          shared "default" value across independent processes absent a
          session context (see _create_sandbox's docstring). Two or more
          matches means we cannot tell which one is really "this" task's,
          so we refuse to guess and create fresh instead. This carries the
          same task_id-collision profile Daytona's own resume-by-name
          already accepts in this codebase -- not a new risk class.
        - A match that ISN'T "stopped" (e.g. still "running") is left
          alone rather than attached to -- that state means something else
          may actively be using it right now.
        """
        try:
            candidates = [
                sb for sb in Sandbox.list(tags={"hermes_task_id": task_id})
                if sb.state == "stopped"
            ]
        except Exception as e:
            logger.warning("DeepInfra: resume lookup failed for task %s: %s", task_id, e)
            return None

        if not candidates:
            return None
        if len(candidates) > 1:
            logger.warning(
                "DeepInfra: %d stopped sandboxes match task %s -- ambiguous, "
                "creating a new one instead of guessing which to resume",
                len(candidates), task_id,
            )
            return None

        sandbox = candidates[0]
        try:
            sandbox.start()
        except Exception as e:
            logger.warning("DeepInfra: failed to resume sandbox %s: %s", sandbox.id, e)
            return None
        logger.info("DeepInfra: resumed sandbox %s for task %s", sandbox.id, task_id)
        return sandbox

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

        stdin_data = kwargs.pop("stdin_data", None)
        if stdin_data:
            command = self._pipe_stdin_via_remote_temp(command, stdin_data)
        return super().execute(command, cwd, **kwargs)

    def _pipe_stdin_via_remote_temp(self, command: str, stdin_data: str) -> str:
        """Deliver ``stdin_data`` to *command* without BaseEnvironment's
        heredoc embedding (``_stdin_mode = "heredoc"``), which has two bugs
        found live against a multi-statement script (hermes-agent's own
        atomic ``write_file``, ``tools/file_operations.py``'s
        ``_atomic_write``):

        1. ``_embed_stdin_heredoc`` appends ``<< 'DELIM'`` to the END of the
           whole command string. A heredoc redirect binds to the LAST simple
           command in a ``;``-separated script, not to whichever earlier
           statement actually reads stdin (``cat > "$tmp"`` in
           ``_atomic_write``) -- so the real target of the write never gets
           its input, and instead blocks reading the sandbox exec call's own
           (never-EOF'd) stdin until the full command timeout elapses. Live
           reproduction: a 29-byte write hung for the entire configured
           timeout (180s in a real agent run; reproduced deterministically
           down to 15s) before failing with "Command timed out".
        2. Even for a single simple command where #1 doesn't apply, a
           heredoc body MUST end with a newline before its closing
           delimiter -- so any content that doesn't already end in ``\\n``
           silently gains one on disk. Confirmed live: on-disk sha256
           matched content+"\\n", not the original bytes, tripping
           hermes-agent's own post-write hash verification.

        Fix: upload stdin_data to a small remote temp file via fs.write()
        (byte-exact, no shell quoting or heredoc-fidelity concerns at all),
        then pipe that file's content into a ``{ command; }`` GROUP. A pipe
        correctly delivers its stream to whichever single statement inside
        the group reads stdin, regardless of how many other statements
        precede or follow it in the group -- unlike a heredoc, which binds
        to one specific (and here, wrong) statement. Piping from an
        uploaded file also means the command string itself only ever
        carries a short temp path, not the content -- deep_sands' exec()
        has no stdin field at all (nothing rides outside the command
        string either way), so this avoids adding any argv-size exposure
        beyond what the temp path costs.

        Known gap: BaseEnvironment.execute()'s sudo_stdin merging (from
        _prepare_command) runs AFTER this override returns and never sees
        stdin_data here, so it can't be combined with a sudo password
        prompt's stdin. Not a realistic scenario for deep_sands sandboxes
        (single-user root-equivalent microVMs, no interactive sudo prompts
        in practice) -- not handled.
        """
        remote_tmp = f"{_SYNC_DIR}/{uuid.uuid4().hex}.stdin"
        self._sandbox.fs.write(remote_tmp, stdin_data.encode("utf-8", errors="surrogateescape"))
        q_tmp = shlex.quote(remote_tmp)
        return (
            f"cat {q_tmp} | {{ {command}\n}}; "
            f"__hermes_stdin_ec=$?; rm -f {q_tmp}; "
            f"( exit $__hermes_stdin_ec )"
        )

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
                if self._persistent:
                    # Stop, don't terminate: this is also what runs on every
                    # idle-reap (terminal.lifetime_seconds, default 300s),
                    # not just real session end. Terminating here would
                    # destroy /workspace after 5 idle minutes; stopping
                    # preserves it for _try_resume_sandbox() on the next
                    # construction, mirroring Daytona's own persistent
                    # cleanup() (tools/environments/daytona.py).
                    self._sandbox.stop()
                    logger.info(
                        "DeepInfra: stopped sandbox %s (filesystem preserved)",
                        self._sandbox.id,
                    )
                else:
                    self._sandbox.terminate()
                    logger.info("DeepInfra: terminated sandbox %s", self._sandbox.id)
            except Exception as e:
                # Do NOT drop the handle here: stop()/terminate() failing is
                # indeterminate, not confirmation the sandbox is gone/stopped.
                # If we null self._sandbox unconditionally, a transient
                # failure permanently loses the only reference needed to
                # retry -- the sandbox may still exist and still be billing.
                # Leaving self._sandbox set means a subsequent cleanup() call
                # (the idle reaper retries, or the caller retries explicitly)
                # naturally retries against the exact same sandbox, not a
                # tag-based re-lookup.
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
