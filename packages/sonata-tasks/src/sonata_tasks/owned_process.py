"""Bounded argv execution with an isolated Linux descendant owner.

No shell, global process-name matching, or changes to the caller's subreaper
state. The supervisor signals only children whose current parent is itself,
using pidfds; killing an ancestor adopts even setsid descendants into that set.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from typing import Protocol

MIN_STOP_TIMEOUT_S = 0.1


_SUPERVISOR = r"""
import ctypes, json, os, selectors, signal, subprocess, sys, time
from pathlib import Path
cfg = json.loads(sys.argv[1])
child = None
log = None
summary = None
selector = selectors.DefaultSelector()
result = dict(returncode=None, forced_stop=False, reaped=False, cancelled=False,
    timed_out=False, quota_exceeded=False, log_bytes=0, summary_bytes=0,
    summary_complete=False, errors=[])
control = b''
buffer = b''
mode = 'log'
marker = cfg.get('summary_marker')
start = ('\n' + marker + ':START\n').encode() if marker else None
end = ('\n' + marker + ':END\n').encode() if marker else None
remaining = cfg['output_limit_bytes']
stop_at = None
kill_at = None
cleanup_at = None
descendants_at = None
final_window_s = cfg['stop_timeout_s'] * .3
sent_interrupt = False

def error(message):
    if len(result['errors']) < 8:
        result['errors'].append(str(message)[:512])

def identity(pid):
    try:
        fields = Path('/proc/%s/stat' % pid).read_text().rsplit(')', 1)[1].split()
        return int(fields[1]), fields[19], fields[0]
    except (FileNotFoundError, ProcessLookupError):
        return None

def children():
    raw = Path('/proc/self/task/%s/children' % os.getpid()).read_text()
    return [int(pid) for pid in raw.split()]

def owned_signal(pid, sig):
    before = identity(pid)
    if before is None or before[0] != os.getpid() or before[2] == 'Z':
        return False
    try:
        fd = os.pidfd_open(pid, 0)
    except ProcessLookupError:
        return False
    try:
        after = identity(pid)
        if after is None or after[:2] != before[:2]:
            return False
        signal.pidfd_send_signal(fd, sig)
        return True
    except ProcessLookupError:
        return False
    finally:
        os.close(fd)

def reap():
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return True
        if pid == 0:
            return False
        if child is not None and pid == child.pid:
            child.returncode = os.waitstatus_to_exitcode(status)
            result['returncode'] = child.returncode

def begin_stop(timeout, cancelled=False, immediate=False):
    global stop_at, kill_at, cleanup_at, final_window_s
    now = time.monotonic()
    result['cancelled'] |= cancelled
    stop_at = now if stop_at is None else stop_at
    grace = 0 if immediate else timeout * .25
    kill_at = min(kill_at if kill_at is not None else float('inf'), now + grace)
    # Keep a separate final adoption/reap window. Reusing an expired main-loop
    # deadline permits just one kill wave and abandons newly adopted descendants.
    cleanup_at = min(
        cleanup_at if cleanup_at is not None else float('inf'), now + timeout * .55
    )
    final_window_s = min(final_window_s, timeout * .3)

def read_control():
    global control
    try:
        part = os.read(0, 256)
    except BlockingIOError:
        return
    if not part:
        # The caller disappeared or abandoned its pipe: retain ownership until
        # the same bounded cleanup has finished, even without an Event signal.
        begin_stop(cfg['stop_timeout_s'], cancelled=True)
        return
    control += part
    while b'\n' in control:
        line, control = control.split(b'\n', 1)
        begin_stop(float(line), cancelled=True)
    if len(control) > 256:
        raise RuntimeError('oversized supervisor control message')

def write_part(part):
    global remaining
    if not part:
        return
    written = min(len(part), remaining)
    if written:
        target = summary if mode == 'summary' else log
        target.write(part[:written])
        result['summary_bytes' if mode == 'summary' else 'log_bytes'] += written
        remaining -= written
    if written != len(part):
        result['quota_exceeded'] = True
        if stop_at is None:
            begin_stop(cfg['stop_timeout_s'], immediate=True)

def consume(part, final=False):
    global buffer, mode, summary
    buffer += part
    while buffer:
        delimiter = start if mode == 'log' else end if mode == 'summary' else None
        if delimiter is None:
            write_part(buffer)
            buffer = b''
            return
        position = buffer.find(delimiter)
        if position >= 0:
            write_part(buffer[:position])
            buffer = buffer[position + len(delimiter):]
            if mode == 'log':
                summary = open(cfg['summary_path'], 'xb', buffering=0)
                mode = 'summary'
            else:
                result['summary_complete'] = True
                mode = 'after'
            continue
        safe = len(buffer) if final else max(0, len(buffer) - len(delimiter) + 1)
        write_part(buffer[:safe])
        buffer = buffer[safe:]
        return

try:
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise RuntimeError('cannot enable owned descendant reaping')
    probe = os.pidfd_open(os.getpid(), 0)
    os.close(probe)
    os.set_blocking(0, False)
    read_control()
    log = open(cfg['log_path'], 'xb', buffering=0)
    if stop_at is None:
        child = subprocess.Popen(cfg['argv'], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        os.set_blocking(child.stdout.fileno(), False)
        selector.register(child.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + cfg['timeout_s']
        eof = False
        while True:
            read_control()
            now = time.monotonic()
            code = child.poll()
            if code is not None:
                result['returncode'] = code
                if cleanup_at is None:
                    cleanup_at = now + cfg['stop_timeout_s'] * .55
                if descendants_at is None:
                    # This supervisor is a subreaper, so `children()` here holds
                    # whatever the leader orphaned -- including a single-use
                    # helper that is *designed* to stop once the leader exits
                    # (Gradle's `--no-daemon` daemon announces exactly that).
                    # SIGKILLing it in the same iteration the leader exits calls
                    # a clean build a forced stop. Give adopted children a
                    # bounded grace to leave on their own; whatever is still
                    # alive after it is killed and confirmed as before, so
                    # `forced_stop` keeps meaning something had to be forced.
                    descendants_at = now + cfg['stop_timeout_s'] * .3
                elif now >= descendants_at:
                    for pid in children():
                        result['forced_stop'] |= owned_signal(pid, signal.SIGKILL)
            elif now >= deadline and stop_at is None:
                result['timed_out'] = True
                begin_stop(cfg['stop_timeout_s'])
            if stop_at is not None:
                if not sent_interrupt and now < kill_at:
                    owned_signal(child.pid, signal.SIGINT)
                    sent_interrupt = True
                if now >= kill_at:
                    for pid in children():
                        result['forced_stop'] |= owned_signal(pid, signal.SIGKILL)
            result['reaped'] = (
                reap() if code is not None or stop_at is not None else False
            )
            for key, _ in selector.select(.005):
                try:
                    part = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if part:
                    consume(part)
                else:
                    eof = True
                    selector.unregister(key.fileobj)
                    consume(b'', final=True)
            if result['reaped'] and eof:
                break
            if cleanup_at is not None and time.monotonic() >= cleanup_at:
                error('owned cleanup or output drain exceeded deadline')
                break
    else:
        result['reaped'] = True
except BaseException as exc:
    error('%s: %s' % (type(exc).__name__, exc))
finally:
    # Exceptional paths use the SAME verified direct-child wave, never killpg.
    if not result['reaped']:
        final_deadline = time.monotonic() + final_window_s
        while True:
            try:
                for pid in children():
                    result['forced_stop'] |= owned_signal(pid, signal.SIGKILL)
                result['reaped'] = reap()
            except BaseException as exc:
                error('cleanup: %s' % exc)
                break
            if result['reaped'] or time.monotonic() >= final_deadline:
                break
            time.sleep(.002)
    if child is not None and child.returncode is not None:
        result['returncode'] = child.returncode
    if not result['reaped']:
        error('owned descendants not fully reaped')
    if log is not None:
        log.close()
    if summary is not None:
        summary.close()
    selector.close()
    result['ended_s'] = time.monotonic() if result['reaped'] else None
    os.write(1, json.dumps(result, separators=(',', ':')).encode())
"""


class _Cancellation(Protocol):
    """Cancellation subset shared by Events and deterministic test doubles."""

    def is_set(self) -> bool: ...

    def wait(self, timeout: float) -> object: ...


@dataclass(frozen=True)
class OwnedCommandResult:
    """Record command outcome, output bounds, and confirmed descendant cleanup."""

    returncode: int | None
    forced_stop: bool
    reaped: bool
    cancelled: bool = False
    timed_out: bool = False
    quota_exceeded: bool = False
    log_bytes: int = 0
    summary_bytes: int = 0
    summary_complete: bool = False
    ended_s: float | None = None
    errors: tuple[str, ...] = ()


def _positive(value: float, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be finite and positive")


def _stop_budget(value: float) -> None:
    _positive(value, "stop_timeout_s")
    if value < MIN_STOP_TIMEOUT_S:
        raise ValueError(
            f"stop_timeout_s must be at least {MIN_STOP_TIMEOUT_S}s "
            "for descendant cleanup"
        )


class OwnedCommandRunner:
    """Stoppable runner; summary shares the log budget; cleanup needs >= 0.1s."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str,
        env: Mapping[str, str],
        log_path: Path,
        timeout_s: float,
        cancelled: _Cancellation,
        output_limit_bytes: int,
        stop_timeout_s: float = 1.0,
        summary_path: Path | None = None,
        summary_marker: str | None = None,
    ) -> None:
        """Validate command ownership, output policy, and supported cleanup bounds."""
        if sys.platform != "linux" or not hasattr(os, "pidfd_open"):
            raise ValueError("owned command execution requires Linux pidfds and procfs")
        if (
            isinstance(argv, str)
            or not argv
            or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
        ):
            raise ValueError("argv must be a nonempty argument sequence")
        _positive(timeout_s, "timeout_s")
        _stop_budget(stop_timeout_s)
        if type(output_limit_bytes) is not int or output_limit_bytes <= 0:
            raise ValueError("output_limit_bytes must be a positive integer")
        if (summary_path is None) != (summary_marker is None):
            raise ValueError("summary path and marker must be supplied together")
        self._settings = {
            "argv": list(argv),
            "log_path": str(Path(log_path).absolute()),
            "timeout_s": timeout_s,
            "output_limit_bytes": output_limit_bytes,
            "stop_timeout_s": stop_timeout_s,
            "summary_marker": summary_marker,
            "summary_path": str(Path(summary_path).absolute())
            if summary_path
            else None,
        }
        self._cwd, self._env = cwd, dict(env)
        self._cancelled = cancelled
        self._stopped = Event()
        self._lock = Lock()
        self._process: subprocess.Popen | None = None
        self._used = False

    @property
    def launched(self) -> bool:
        """Whether `run` reached a child process.

        A failure while this is false provably occurred before the supervised
        command was started, so nothing of the caller's was ever touched.
        """
        return self._process is not None

    def stop(self, timeout_s: float) -> None:
        """Request bounded graceful stop and adoption-based escalation."""
        _stop_budget(timeout_s)
        with self._lock:
            self._stopped.set()
            process = self._process
            if process is None or process.poll() is not None:
                return
            try:
                if process.stdin is None:
                    raise RuntimeError("command process has no stdin pipe")
                process.stdin.write(f"{timeout_s:g}\n".encode())
                process.stdin.flush()
            except BrokenPipeError:
                pass
        try:
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "owned supervisor exceeded stop deadline; cleanup unconfirmed"
            ) from exc

    def run(self) -> OwnedCommandResult:
        """Execute once and retain bounded output plus the owned cleanup result."""
        with self._lock:
            if self._used:
                raise RuntimeError("owned command runner cannot be reused")
            self._used = True
            if self._cancelled.is_set() or self._stopped.is_set():
                return OwnedCommandResult(
                    None, False, True, cancelled=True, ended_s=time.monotonic()
                )
            Path(self._settings["log_path"]).parent.mkdir(parents=True, exist_ok=True)
            self._process = subprocess.Popen(
                [sys.executable, "-c", _SUPERVISOR, json.dumps(self._settings)],
                cwd=self._cwd,
                env=self._env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        process = self._process
        hard_deadline = (
            time.monotonic()
            + self._settings["timeout_s"]
            + self._settings["stop_timeout_s"]
            + 1
        )
        try:
            while process.poll() is None:
                if self._cancelled.is_set() or self._stopped.is_set():
                    self.stop(self._settings["stop_timeout_s"])
                    break
                if time.monotonic() >= hard_deadline:
                    self.stop(self._settings["stop_timeout_s"])
                    break
                self._cancelled.wait(0.01)
            # Only trusted supervisor metadata reaches this pipe, never child output.
            if process.stdout is None:
                raise RuntimeError("command process has no stdout pipe")
            raw = process.stdout.read(8193)
            if len(raw) > 8192:
                raise RuntimeError("oversized supervisor receipt")
            try:
                data = json.loads(raw)
                data["errors"] = tuple(data.get("errors", ()))
                return OwnedCommandResult(**data)
            except (ValueError, TypeError) as exc:
                raise RuntimeError(
                    "owned supervisor failed without a valid cleanup receipt"
                ) from exc
        except BaseException as exc:
            try:
                self.stop(self._settings["stop_timeout_s"])
            except Exception as cleanup_error:
                exc.add_note(f"owned cleanup: {cleanup_error}")
            raise
        finally:
            for stream in (process.stdin, process.stdout):
                if stream is None:
                    continue
                with contextlib.suppress(OSError):
                    stream.close()


def run_owned_command(
    argv: Sequence[str],
    *,
    cwd: Path | str,
    env: Mapping[str, str],
    log_path: Path,
    timeout_s: float,
    cancelled: _Cancellation,
    output_limit_bytes: int,
) -> OwnedCommandResult:
    """Execute argv locally, retaining bounded combined stdout/stderr in log_path.

    Nonzero command exits are returned as evidence. Cancellation, timeout, quota
    exhaustion, forced descendant cleanup or an unreaped result are never success.
    No output is loaded into the caller's memory; only bounded metadata is returned.
    """
    return OwnedCommandRunner(
        argv,
        cwd=cwd,
        env=env,
        log_path=log_path,
        timeout_s=timeout_s,
        cancelled=cancelled,
        output_limit_bytes=output_limit_bytes,
    ).run()
