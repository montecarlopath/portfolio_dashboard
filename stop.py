"""Stop script for Portfolio Dashboard.

Kills the backend (uvicorn on port 8000), frontend (next on port 3000),
and any orphan processes left behind.

Works on macOS and Linux. Falls back to Windows commands if neither lsof
nor ps are available (unlikely but handled).

Usage: python stop.py [--backend-port 8000] [--frontend-port 3000]
"""

import argparse
import os
import platform
import signal
import subprocess
import sys
import time


IS_WINDOWS = platform.system() == "Windows"
IS_MAC_OR_LINUX = not IS_WINDOWS


# ── Port helpers ──────────────────────────────────────────────────────────────

def get_pids_on_port(port: int) -> list[int]:
    """Return all PIDs listening on the given port."""
    pids: set[int] = set()

    if IS_MAC_OR_LINUX:
        # lsof is reliable on macOS and Linux
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.add(int(line))
        except Exception:
            pass

        # Fallback: ss (Linux) or netstat
        if not pids:
            try:
                result = subprocess.run(
                    ["netstat", "-anp", "tcp"],
                    capture_output=True, text=True, timeout=10,
                )
                for line in result.stdout.splitlines():
                    if f".{port} " in line or f":{port} " in line:
                        parts = line.split()
                        for p in parts:
                            if "/" in p:
                                pid_str = p.split("/")[0]
                                if pid_str.isdigit():
                                    pids.add(int(pid_str))
            except Exception:
                pass

    else:
        # Windows: netstat -ano
        try:
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line:
                    parts = line.split()
                    if len(parts) >= 5:
                        try:
                            pids.add(int(parts[-1]))
                        except ValueError:
                            pass
        except Exception:
            pass

    pids.discard(0)
    return sorted(pids)


# ── Process name lookup ───────────────────────────────────────────────────────

def get_process_name(pid: int) -> str:
    """Return the process name for a PID."""
    try:
        if IS_MAC_OR_LINUX:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "comm="],
                capture_output=True, text=True, timeout=5,
            )
            return result.stdout.strip()
        else:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            parts = result.stdout.strip().strip('"').split('"')
            return parts[0] if parts else ""
    except Exception:
        return "unknown"


def get_process_cmdline(pid: int) -> str:
    """Return the full command line for a PID."""
    try:
        if IS_MAC_OR_LINUX:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "args="],
                capture_output=True, text=True, timeout=5,
            )
            return result.stdout.strip()
    except Exception:
        pass
    return ""


# ── Kill helpers ──────────────────────────────────────────────────────────────

def kill_pid(pid: int, force: bool = False, wait_secs: float = 1.0) -> bool:
    """
    Kill a process. Returns True if the process no longer exists after the call.
    Tries SIGTERM first, then SIGKILL (or taskkill on Windows).
    """
    try:
        if IS_MAC_OR_LINUX:
            sig = signal.SIGKILL if force else signal.SIGTERM
            os.kill(pid, sig)
            if not force:
                time.sleep(wait_secs)
                # Check if still alive; if so, force kill
                try:
                    os.kill(pid, 0)   # signal 0 = existence check
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass             # already gone — good
        else:
            if force:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True, timeout=10,
                )
            else:
                os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    except Exception:
        pass

    # Verify it's gone
    try:
        os.kill(pid, 0)
        return False   # still alive
    except (ProcessLookupError, OSError):
        return True    # gone


def get_child_pids(pid: int) -> list[int]:
    """Return all direct child PIDs of a process (macOS/Linux)."""
    children = []
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                child = int(line)
                children.append(child)
                children.extend(get_child_pids(child))   # recurse
    except Exception:
        pass
    return children


def kill_tree(pid: int) -> int:
    """Kill a process and all its descendants. Returns number killed."""
    killed = 0
    children = get_child_pids(pid)
    for child in reversed(children):
        if kill_pid(child, force=True):
            killed += 1
    if kill_pid(pid, force=False):
        killed += 1
    return killed


# ── Named-process finders (macOS/Linux) ──────────────────────────────────────

def find_pids_by_cmdline(*patterns: str) -> list[int]:
    """
    Find PIDs whose command line contains ALL of the given patterns.
    Uses pgrep -f on macOS/Linux.
    """
    if not IS_MAC_OR_LINUX:
        return []

    # Build a combined grep pattern by passing to pgrep -f
    # For multiple patterns we filter ourselves using ps
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,args"],
            capture_output=True, text=True, timeout=10,
        )
        found = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) < 2:
                continue
            pid_str, cmdline = parts
            if not pid_str.isdigit():
                continue
            if all(p.lower() in cmdline.lower() for p in patterns):
                pid = int(pid_str)
                if pid != os.getpid():   # never kill ourselves
                    found.append(pid)
        return found
    except Exception:
        return []


# ── Main stop logic ───────────────────────────────────────────────────────────

def kill_on_port(label: str, port: int) -> int:
    """Kill everything on a port. Returns number of processes killed."""
    pids = get_pids_on_port(port)
    if not pids:
        print(f"  No processes found on port {port}")
        return 0

    killed = 0
    for pid in pids:
        name = get_process_name(pid)
        n = kill_tree(pid)
        if n:
            print(f"  Killed PID {pid} ({name}) and {n - 1} child(ren)")
            killed += n
        else:
            print(f"  Could not kill PID {pid} ({name}) — may have already exited")
    return killed


def kill_orphan_uvicorn() -> int:
    """Kill any uvicorn processes not already caught by port scan."""
    killed = 0
    # Match: python ... uvicorn app.main  OR  uvicorn app.main directly
    pids = find_pids_by_cmdline("uvicorn", "app.main")
    pids += find_pids_by_cmdline("uvicorn", "app:main")
    pids = list(set(pids))

    for pid in pids:
        cmdline = get_process_cmdline(pid)
        n = kill_tree(pid)
        if n:
            print(f"  Killed orphan uvicorn PID {pid}: {cmdline[:80]}")
            killed += n
    return killed


def kill_orphan_next() -> int:
    """Kill any next-dev / next dev processes not already caught by port scan."""
    killed = 0
    pids = find_pids_by_cmdline("next", "dev")
    pids += find_pids_by_cmdline("next-server")
    pids = list(set(pids))

    for pid in pids:
        cmdline = get_process_cmdline(pid)
        n = kill_tree(pid)
        if n:
            print(f"  Killed orphan next PID {pid}: {cmdline[:80]}")
            killed += n
    return killed


# ── Entry point ───────────────────────────────────────────────────────────────

def main(args: argparse.Namespace):
    backend_port = int(args.backend_port)
    frontend_port = int(args.frontend_port)

    print("=" * 50)
    print("  Stopping Portfolio Dashboard")
    print("=" * 50)
    total = 0

    print(f"\nBackend (port {backend_port}):")
    total += kill_on_port("backend", backend_port)

    print(f"\nFrontend (port {frontend_port}):")
    total += kill_on_port("frontend", frontend_port)

    print("\nOrphan uvicorn processes:")
    count = kill_orphan_uvicorn()
    if not count:
        print("  None found")
    total += count

    print("\nOrphan next-dev processes:")
    count = kill_orphan_next()
    if not count:
        print("  None found")
    total += count

    # Final sweep — anything still alive on our ports?
    time.sleep(0.5)
    remaining = list(set(get_pids_on_port(backend_port) + get_pids_on_port(frontend_port)))
    if remaining:
        print(f"\nForce-killing {len(remaining)} stubborn process(es)...")
        for pid in remaining:
            name = get_process_name(pid)
            if kill_pid(pid, force=True):
                print(f"  Force-killed PID {pid} ({name})")
                total += 1
            else:
                print(f"  WARNING: PID {pid} ({name}) could not be killed — try: sudo kill -9 {pid}")

    # Confirm ports are free
    still_busy = []
    for port in [backend_port, frontend_port]:
        if get_pids_on_port(port):
            still_busy.append(port)

    print(f"\nDone. {total} process(es) stopped.")

    if still_busy:
        print(f"\nWARNING: port(s) {still_busy} still in use.")
        print("Try running with sudo, or wait a few seconds and retry.")
        sys.exit(1)
    else:
        print("All ports are free.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stop Portfolio Dashboard backend/frontend and orphan processes."
    )
    parser.add_argument("--backend-port", type=int, default=8000)
    parser.add_argument("--frontend-port", type=int, default=3000)
    return parser.parse_args()


if __name__ == "__main__":
    main(parse_args())