#!/usr/bin/env python3
"""cec_scheduler.py

Schedules CEC commands (via the `cec-client` binary) based on a YAML
configuration file.

Requirements:
  pip install pyyaml apscheduler

Usage:
  python3 cec_scheduler.py --config cec_schedule.yaml
  python3 cec_scheduler.py --config cec_schedule.yaml --dry-run

Config format (YAML):

device: 0
cec_client_path: /usr/bin/cec-client  # optional, default: cec-client
commands:
  - time: "15:00"
    command: "on"
  - time: "20:00"
    command: "standby"
    days: [mon,tue,wed,thu,fri]

Fields:
  - time: "HH:MM" or "HH:MM:SS"
  - command: string (e.g. on, standby, tx 44:41:...)
  - days: optional list of days (mon,tue,...,sun). If omitted, runs every day.
  - retries: optional int, extra times to re-send each command (default 0).
    Useful for waking a TV that has been in deep standby for a while.
  - retry_delay: optional float, seconds between attempts (default 10).

Note: cec-client is run WITHOUT ``-s`` and given a few seconds to initialize
before each command, so wake commands still work after the TV has dropped off
the CEC bus.

"""

from __future__ import annotations

import argparse
import logging
import signal
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional
import time

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

LOG = logging.getLogger("cec_scheduler")


@dataclass
class ScheduledCommand:
    time: str  # "HH:MM" or "HH:MM:SS"
    commands: List[str]
    days: Optional[List[str]] = None
    name: Optional[str] = None
    retries: int = 0  # extra times to re-send each command (helps wake a TV in deep standby)
    retry_delay: float = 10.0  # seconds between attempts


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg


def parse_time(t: str) -> tuple[int, int, int]:
    parts = t.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"Invalid time format: {t}")
    h = int(parts[0])
    m = int(parts[1])
    s = int(parts[2]) if len(parts) == 3 else 0
    return h, m, s


def run_cec_client(
    cec_client_path: str,
    stdin_text: str,
    dry_run: bool = False,
    init_wait: float = 4.0,
    settle_wait: float = 3.0,
) -> tuple[int, str, str]:
    """Runs cec-client with the provided stdin and returns (returncode, stdout, stderr).

    Deliberately does NOT use ``-s`` (single-command) mode: after the TV has been
    in deep standby for a few minutes, the CEC adapter needs time to re-power,
    poll the bus and register a logical address. ``-s`` transmits before that
    handshake finishes, so wake commands silently get dropped. Instead we start
    cec-client normally, wait ``init_wait`` seconds for it to initialize, send the
    command, wait ``settle_wait`` for it to be transmitted, then close stdin so
    cec-client exits.
    """
    LOG.debug("Will run cec-client: %s; stdin: %s", cec_client_path, stdin_text.strip())
    if dry_run:
        print(f"DRY-RUN: {cec_client_path} -d 1 <<< {stdin_text!r}")
        return 0, "", ""

    try:
        p = subprocess.Popen(
            [cec_client_path, "-d", "1"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        LOG.error("cec-client not found at %s", cec_client_path)
        return 127, "", ""

    try:
        time.sleep(init_wait)
        assert p.stdin is not None
        p.stdin.write(stdin_text)
        p.stdin.flush()
        time.sleep(settle_wait)
        p.stdin.close()
        stdout, stderr = p.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        p.kill()
        stdout, stderr = p.communicate()
    except Exception:
        p.kill()
        p.communicate()
        raise

    stdout = stdout or ""
    stderr = stderr or ""
    LOG.debug("cec-client exited: %s", p.returncode)
    if stdout.strip():
        LOG.debug("cec-client stdout: %s", stdout.strip())
    if stderr.strip():
        LOG.debug("cec-client stderr: %s", stderr.strip())
    return p.returncode, stdout, stderr


def build_stdin_for_command(cmd: str, device: str) -> str:
    # Common short commands: 'on', 'standby'
    # If the command already contains the device, pass it through
    parts = cmd.strip().split()
    if parts[0] in ("on", "standby", "power") and len(parts) == 1:
        return f"{parts[0]} {device}\n"
    # allow 'tx 44:41:..' or any other cec-client input
    return cmd.strip() + "\n"


def schedule_commands(scheduler: BackgroundScheduler, cfg: Dict[str, Any], dry_run: bool = False):
    device = str(cfg.get("device", "0"))
    cec_client_path = str(cfg.get("cec_client_path", "cec-client"))

    raw_cmds = cfg.get("commands", [])
    if not raw_cmds:
        LOG.warning("No commands found in configuration")

    global_delay = float(cfg.get("command_delay", 1.0))
    global_retries = int(cfg.get("retries", 0))
    global_retry_delay = float(cfg.get("retry_delay", 10.0))

    for idx, item in enumerate(raw_cmds):
        # Support either 'commands' (list) or legacy 'command' (string)
        raw_commands = item.get("commands")
        if raw_commands is None:
            if "command" in item:
                raw_commands = [item["command"]]
            else:
                LOG.warning("Skipping schedule item %s: no 'command' or 'commands' found", item)
                continue
        # Normalize to list of strings
        commands_list = [str(c) for c in raw_commands] if isinstance(raw_commands, (list, tuple)) else [str(raw_commands)]

        # Per-item delay (seconds) between commands
        item_delay = float(item.get("command_delay", global_delay))

        sc = ScheduledCommand(
            time=item["time"],
            commands=commands_list,
            days=item.get("days"),
            name=item.get("name") or (commands_list[0] if commands_list else f"cmd-{idx}"),
            retries=int(item.get("retries", global_retries)),
            retry_delay=float(item.get("retry_delay", global_retry_delay)),
        )
        h, m, s = parse_time(sc.time)

        day_of_week = None
        if sc.days:
            # Expect days like: mon, tue, wed, thu, fri, sat, sun
            day_of_week = ",".join(sc.days)

        # Ensure the trigger uses the scheduler's timezone (local time)
        trigger = CronTrigger(hour=h, minute=m, second=s, day_of_week=day_of_week, timezone=scheduler.timezone)

        def job_wrapper(sc=sc, device=device, cec_client_path=cec_client_path, dry_run=dry_run, item_delay=item_delay):
            LOG.info("Executing scheduled job '%s' at %s (commands=%s)", sc.name, datetime.now().astimezone(), sc.commands)
            attempts = sc.retries + 1
            for i, cmd in enumerate(sc.commands):
                stdin = build_stdin_for_command(cmd, device)
                for attempt in range(1, attempts + 1):
                    LOG.info("Command %d/%d: %s (attempt %d/%d)", i + 1, len(sc.commands), cmd, attempt, attempts)
                    rc, out, err = run_cec_client(cec_client_path, stdin, dry_run=dry_run)
                    if rc == 0:
                        LOG.info("Command '%s' triggered successfully for device %s at %s", cmd, device, datetime.now().astimezone())
                        if out.strip():
                            LOG.info("Command output: %s", out.strip())
                    else:
                        LOG.warning("Command '%s' returned non-zero exit status %s; stderr: %s", cmd, rc, err.strip())

                    if attempt < attempts and sc.retry_delay > 0:
                        LOG.debug("Sleeping %.2fs before retry", sc.retry_delay)
                        time.sleep(sc.retry_delay)

                # Delay before the next command if applicable
                if i < len(sc.commands) - 1 and item_delay > 0:
                    LOG.debug("Sleeping %.2fs before next command", item_delay)
                    time.sleep(item_delay)

        scheduler.add_job(job_wrapper, trigger=trigger, id=f"job-{idx}", name=sc.name)
        LOG.info("Scheduled '%s' at %s (days: %s) commands: %s", sc.name, sc.time, sc.days or 'everyday', sc.commands)


def parse_args():
    p = argparse.ArgumentParser(description="Schedule CEC commands using cec-client and a YAML config.")
    p.add_argument("--config", "-c", required=True, help="Path to YAML configuration file")
    p.add_argument("--dry-run", action="store_true", help="Print commands instead of executing")
    p.add_argument("--loglevel", default="INFO", help="Logging level (DEBUG, INFO, WARNING, ERROR)")
    return p.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.loglevel.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(message)s")

    # When run under systemd, stdout/stderr is already captured into the journal,
    # so no extra journal/syslog handler is attached (doing so double-logs lines).

    cfg = load_config(args.config)

    # Use system local timezone for scheduling (so YAML times are local time, not UTC)
    local_tz = datetime.now().astimezone().tzinfo
    LOG.info("Using local timezone for scheduling: %s", local_tz)
    scheduler = BackgroundScheduler(timezone=local_tz)
    schedule_commands(scheduler, cfg, dry_run=args.dry_run)

    def shutdown(signum, frame):
        LOG.info("Shutting down scheduler (signal %s)", signum)
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    scheduler.start()
    LOG.info("Scheduler started. Press Ctrl+C to exit.")

    try:
        # Keep the main thread alive.
        while True:
            signal.pause()
    except AttributeError:
        # Windows or systems without signal.pause
        import time

        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
