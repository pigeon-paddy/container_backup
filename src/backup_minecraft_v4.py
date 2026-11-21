#!/usr/bin/env python3
"""
Universal Docker-volume backup (stop container -> tar.gz -> start container -> webhook).

USE: backup_minecraft_v4.py <repr> <container_name> <container_path> [options]

  repr             Short server name, e.g. mc3_rs. Backups go to <backup-root>/<repr>/
                   and are named <repr>_<YYYY-mm-dd_HH-MM-SS>.tar.gz
  container_name   Docker container to stop/start, e.g. mc-backend-rs
  container_path   Server data directory as seen inside THIS container, e.g. /servers/data_rs

Environment variables (all optional, flags override them):
  BACKUP_ROOT (/backup)   KEEP (7)   STOP_TIMEOUT (60)
  WEBHOOK_URL (n8n webhook)           DOCKER_SOCKET (/var/run/docker.sock)

Exit codes: 0 = ok (or skipped because already running), 1 = backup/container/preflight error, 2 = usage error.
Needs only the Python standard library plus the `tar` binary (and optionally `pigz`).
"""

import argparse
import fcntl
import http.client
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
DEFAULT_WEBHOOK = os.environ.get("WEBHOOK_URL", "https://n8n.reyvprdctn.de/webhook/mc_v4")
WEBHOOK_USER_AGENT = os.environ.get("WEBHOOK_USER_AGENT", "mc-backup/1.0")

log = logging.getLogger("backup")


class BackupError(Exception):
    """Expected failure with a readable message."""


class State:
    def __init__(self):
        self.was_running = False
        self.stopped = False       # we stopped the container
        self.started = False       # we started it again
        self.success = False       # archive created
        self.file = None
        self.size_mb = None
        self.error = None

    @property
    def ok(self):
        return self.success and (not self.stopped or self.started)


# ---------------------------------------------------------------- Docker API (unix socket)
class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self._unix_path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._unix_path)
        self.sock = sock


def docker_api(method, path, timeout=30):
    conn = UnixHTTPConnection(DOCKER_SOCKET, timeout)
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def container_running(name):
    status, body = docker_api("GET", f"/containers/{urllib.parse.quote(name, safe='')}/json")
    if status == 404:
        raise BackupError(f"Container {name} does not exist.")
    if status != 200:
        raise BackupError(f"Docker inspect failed (HTTP {status}).")
    return bool(json.loads(body).get("State", {}).get("Running"))


def container_stop(name, timeout):
    # Docker sends SIGTERM, waits `timeout` seconds, then SIGKILL. 204 = stopped, 304 = already stopped.
    status, _ = docker_api(
        "POST", f"/containers/{urllib.parse.quote(name, safe='')}/stop?t={timeout}", timeout=timeout + 30
    )
    return status in (204, 304), status


def container_start(name):
    # 204 = started, 304 = already running.
    status, _ = docker_api("POST", f"/containers/{urllib.parse.quote(name, safe='')}/start")
    return status in (204, 304), status


# ---------------------------------------------------------------- tar
def create_archive(source, target, repr_, archive_root, verify):
    pigz = shutil.which("pigz")
    cmd = ["tar", "-I", "pigz"] if pigz else ["tar", "-z"]
    cmd += ["-cf", str(target), "-C", source]
    if archive_root:
        # "./foo" -> "<repr>/foo": extracting creates one folder named after the server
        cmd += ["--transform", f"s,^\\.,{repr_},"]
    cmd.append(".")
    log.info("Creating backup of %s (%s)...", source, "pigz" if pigz else "gzip")

    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    err = proc.stderr.strip()[-500:]
    if proc.returncode == 1:
        log.warning("tar warning (exit 1): %s", err)      # e.g. file changed while reading
    elif proc.returncode != 0:
        raise BackupError(f"tar exited with code {proc.returncode}: {err}")

    if not Path(target).is_file() or Path(target).stat().st_size == 0:
        raise BackupError("tar produced no output file.")

    if verify:
        log.info("Verifying archive...")
        chk = subprocess.run([pigz or "gzip", "-t", str(target)], capture_output=True, text=True)
        if chk.returncode != 0:
            raise BackupError(f"Archive verification failed: {chk.stderr.strip()[-300:]}")


def rotate(dest, repr_, keep):
    pattern = re.compile(rf"^{re.escape(repr_)}_\d{{4}}-\d{{2}}-\d{{2}}_\d{{2}}-\d{{2}}-\d{{2}}\.tar\.gz$")
    backups = sorted((f for f in os.listdir(dest) if pattern.match(f)), reverse=True)
    old = backups[keep:]
    if old:
        log.info("Deleting %d old backup(s) (keeping %d).", len(old), keep)
        for name in old:
            (Path(dest) / name).unlink()


# ---------------------------------------------------------------- webhook
def build_payload(repr_, container, st):
    """Same envelope as the Discord bridge (event/origin/server/message/author),
    plus the top-level state_* keys that the n8n nodes react to."""
    if st.ok:
        text = f"Backup of {repr_} finished: {os.path.basename(st.file)} ({st.size_mb} MB)"
    else:
        text = f"Backup of {repr_} FAILED: {st.error or 'unknown error'}"
    return {
        "event": "backupscript",
        "origin": "backup_script",
        "server": {
            "name": repr_,
            "container": container,
            "bot_id": None,
            "bot_username": None,
        },
        "message": {
            "id": None,
            "content": text,
            "channel_id": None,
            "guild_id": None,
        },
        "author": {"id": None, "username": "backup_script"},
        # --- keep: n8n nodes react directly to these three ---
        "state_backup": "1" if st.success else "0",
        "state_container": "1" if st.started else "0",
        "container_was_running": st.was_running,
        # --- extra info ---
        "repr": repr_,
        "file": os.path.basename(st.file) if st.file else None,
        "size_mb": st.size_mb,
        "error": st.error,
    }


def post_json(url, payload):
    """POST JSON, retry on 5xx/network errors. Returns True on 2xx."""
    data = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        # urllib's default "Python-urllib/3.x" is blocked by Cloudflare (HTTP 403, error code 1010)
        "User-Agent": WEBHOOK_USER_AGENT,
        "Accept": "*/*",
    }
    for attempt in (1, 2, 3):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                log.info("Webhook response: %s", resp.status)
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:300]
            log.error("Webhook returned HTTP %s: %s", exc.code, body)
            if exc.code < 500:          # 4xx (wrong URL, inactive workflow, WAF, ...) will not fix itself
                return False
        except Exception as exc:  # noqa: BLE001
            log.error("Webhook unreachable (attempt %d): %s", attempt, exc)
        if attempt < 3:
            time.sleep(2)
    return False


def preflight_webhook(url):
    """Empty call at the very start: if n8n is not reachable/accepting, do not touch the container."""
    log.info("Preflight webhook call on %s", url)
    return post_json(url, {})


def send_webhook(url, repr_, container, st):
    payload = build_payload(repr_, container, st)
    log.info("Forwarding: %s", payload["event"])
    if not post_json(url, payload):
        log.warning("Warning: webhook could not be sent")


# ---------------------------------------------------------------- main
def parse_args():
    env = os.environ.get
    p = argparse.ArgumentParser(
        usage="%(prog)s <repr> <container_name> <container_path> [options]",
        description="Stop a Docker container, tar.gz its data directory, start it again, notify a webhook.",
    )
    p.add_argument("repr", help="short server name, e.g. mc3_rs")
    p.add_argument("container_name", help="Docker container name")
    p.add_argument("container_path", help="data directory as seen inside this container")
    p.add_argument("--backup-root", default=env("BACKUP_ROOT", "/backup"))
    p.add_argument("--keep", type=int, default=int(env("KEEP", "7")))
    p.add_argument("--stop-timeout", type=int, default=int(env("STOP_TIMEOUT", "60")))
    p.add_argument("--webhook-url", default=env("WEBHOOK_URL", DEFAULT_WEBHOOK))
    p.add_argument("--no-webhook", action="store_true", help="do not call the webhook (also skips the preflight)")
    p.add_argument("--no-preflight", action="store_true", help="skip the empty webhook call at the start")
    p.add_argument("--archive-root", action="store_true",
                   help="put everything inside a top-level folder named <repr> in the archive")
    p.add_argument("--verify", action="store_true", help="test the gzip stream after creating it")
    args = p.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.repr):
        p.error("<repr> may only contain A-Z a-z 0-9 _ . -")
    if args.keep < 1:
        p.error("--keep must be >= 1")
    return args


def setup_logging():
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    )


def main():
    setup_logging()
    args = parse_args()

    # `docker stop` / n8n timeouts send SIGTERM: turn it into an exception so `finally` blocks run
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    # ---- preflight: empty webhook call first; if it fails, abort before anything is touched
    if not args.no_webhook and not args.no_preflight:
        if not preflight_webhook(args.webhook_url):
            log.error("Error: preflight webhook failed - backup aborted, container untouched.")
            return 1

    source = args.container_path.rstrip("/") or "/"
    dest = Path(args.backup_root) / args.repr                    # one folder per server
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_file = dest / f"{args.repr}_{stamp}.tar.gz"
    partial_file = Path(f"{backup_file}.partial")
    st = State()

    # ---- pre-checks BEFORE touching the container
    try:
        if not os.path.isdir(source):
            raise BackupError(f"Source directory {source} does not exist.")
        dest.mkdir(parents=True, exist_ok=True)
    except (BackupError, OSError) as exc:
        st.error = str(exc)
        log.error("Error: %s", st.error)
        if not args.no_webhook:
            send_webhook(args.webhook_url, args.repr, args.container_name, st)
        return 1

    file_handler = logging.FileHandler(dest / "backup.log")
    file_handler.setFormatter(logging.Formatter("%(asctime)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(file_handler)

    # ---- one run per server at a time (lock is released automatically on exit)
    lock_fd = open(dest / ".lock", "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.info("Backup for %s is already running - aborting.", args.repr)
        return 0

    rc = 1
    try:
        try:
            st.was_running = container_running(args.container_name)
            if st.was_running:
                log.info("Stopping container %s (timeout %ds)...", args.container_name, args.stop_timeout)
                ok, status = container_stop(args.container_name, args.stop_timeout)
                if not ok:
                    raise BackupError(f"Container {args.container_name} could not be stopped (HTTP {status}).")
                st.stopped = True
                log.info("Container %s stopped.", args.container_name)
            else:
                log.info("Container %s was not running - it will not be started afterwards.", args.container_name)

            create_archive(source, partial_file, args.repr, args.archive_root, args.verify)
            os.replace(partial_file, backup_file)                # only complete archives get the final name
            st.success = True
            st.file = str(backup_file)
            st.size_mb = round(backup_file.stat().st_size / 1048576, 1)
            log.info("Backup created successfully: %s (%s MB).", backup_file, st.size_mb)

        except BackupError as exc:
            st.error = str(exc)
            log.error("Error: %s", exc)
        except (KeyboardInterrupt, SystemExit):
            st.error = "Interrupted"
            log.error("Error: interrupted")
            raise
        except Exception as exc:  # noqa: BLE001
            st.error = f"Unexpected error: {exc}"
            log.exception("Unexpected error")
        finally:
            partial_file.unlink(missing_ok=True)
            if st.stopped:                                       # ALWAYS bring the container back
                try:
                    ok, status = container_start(args.container_name)
                    if ok:
                        st.started = True
                        log.info("Container %s started successfully.", args.container_name)
                    else:
                        st.error = st.error or f"Container {args.container_name} could not be started (HTTP {status})."
                        log.error("Error: container %s could not be started (HTTP %s)!", args.container_name, status)
                except Exception as exc:  # noqa: BLE001
                    st.error = st.error or f"Container {args.container_name} could not be started: {exc}"
                    log.error("Error: container %s could not be started: %s", args.container_name, exc)

        if st.success:
            try:
                rotate(dest, args.repr, args.keep)
            except OSError as exc:
                log.warning("Warning: rotation failed: %s", exc)

        rc = 0 if st.ok else 1
    finally:
        if not args.no_webhook:
            send_webhook(args.webhook_url, args.repr, args.container_name, st)
    return rc


if __name__ == "__main__":
    sys.exit(main())