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
hdmi_port: 1                           # optional: HDMI port the Pi is plugged into
cec_log_level: 1                       # optional: cec-client -d bitmask (8 = traffic)
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

Top-level options:
  - command_delay: seconds between multiple commands in one item (default 1).
  - retries / retry_delay: defaults for the per-item fields above.
  - hdmi_port: the HDMI port number on the TV the Pi is connected to. Passed to
    cec-client as ``-p`` so it always has a valid physical address, even when it
    starts while the TV is asleep and cannot read EDID. Without this, Roku / ONN
    TVs ignore the ``as`` (<Active Source>) wake frame because its address is
    bogus. Recommended whenever waking a TV from standby.
  - cec_log_level: cec-client ``-d`` log-level bitmask (1=ERROR, 2=WARNING,
    4=NOTICE, 8=TRAFFIC, 16=DEBUG). Default 1. Set to 8 to log every CEC frame
    and its ACK/NAK when diagnosing why a command had no effect.
  - power_check: if true (default), query the TV's CEC power state before each
    job and skip the job (logging an error) if the TV does not answer -- meaning
    it has powered down its CEC controller and is offline. Set false to always
    send commands.

Note: each scheduled job runs a single cec-client process (not ``-s`` mode, and
not one process per command) and waits a few seconds for it to initialize before
transmitting, so wake commands still work after the TV has dropped off the bus
and back-to-back invocations don't fight over the adapter.

"""

from __future__ import annotations

import argparse
import logging
import signal
import subprocess
import sys
import threading
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


def _drain(stream, sink: List[str]) -> None:
    """Read a subprocess stream line-by-line into ``sink`` until EOF."""
    try:
        for line in stream:
            sink.append(line)
    except (ValueError, OSError):
        pass


def parse_power_state(text: str) -> str:
    """Parse cec-client output into 'on' / 'standby' / 'transition' / 'unknown'.

    'unknown' means the TV never answered the ``pow`` query -- typically because
    it has powered down its HDMI-CEC controller and is effectively offline.
    """
    state = "unknown"
    for line in text.lower().splitlines():
        line = line.strip()
        if "power status:" not in line:
            continue
        val = line.split("power status:", 1)[1].strip()
        if "transition" in val:
            state = "transition"
        elif val.startswith("on"):
            state = "on"
        elif val.startswith("standby"):
            state = "standby"
        else:
            state = "unknown"
    return state


def run_cec_job(
    cec_client_path: str,
    commands: List[str],
    device: str,
    *,
    inter_delay: float,
    retries: int,
    retry_delay: float,
    power_check: bool,
    hdmi_port: Optional[str] = None,
    cec_log_level: int = 1,
    dry_run: bool = False,
    init_wait: float = 4.0,
    settle_wait: float = 3.0,
) -> None:
    """Run one long-lived cec-client process and feed it every command for a job.

    Using a single process (rather than one per command) avoids two problems:
      * cec-client can only hold the CEC adapter open one process at a time, so
        back-to-back invocations race and the later one exits before it can
        transmit (this is what produced the "I/O operation on closed file" crash);
      * it does NOT use ``-s`` (single-command) mode, so the adapter gets time to
        re-power, poll the bus and allocate a logical address before we transmit
        -- otherwise wake commands sent to a TV that dropped off the bus are lost.

    We wait ``init_wait`` for that handshake, then write commands (with retries and
    ``inter_delay`` between them) holding stdin open, then wait ``settle_wait`` for
    the last frame to go out before closing stdin so cec-client exits.
    """
    # Base cec-client argv. ``-p`` pins the adapter's physical address to the HDMI
    # port the Pi is plugged into: without it, cec-client that starts while the TV
    # is in standby often can't read EDID, comes up as f.f.f.f, and then its
    # <Active Source> / power-on frames are invalid and silently ignored (this is
    # what stops Roku/ONN TVs from waking). ``-d`` is the log-level bitmask
    # (1=ERROR, 8=TRAFFIC); raise it to see the actual frames and ACK/NAK.
    base_cmd = [cec_client_path, "-d", str(cec_log_level)]
    if hdmi_port:
        base_cmd += ["-p", str(hdmi_port)]

    if dry_run:
        if power_check:
            print(f"DRY-RUN: {' '.join(base_cmd)} <<< 'pow {device}'")
        for cmd in commands:
            print(f"DRY-RUN: {' '.join(base_cmd)} <<< {build_stdin_for_command(cmd, device)!r}")
        return

    try:
        p = subprocess.Popen(
            base_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        LOG.error("cec-client not found at %s", cec_client_path)
        return

    out_lines: List[str] = []
    err_lines: List[str] = []
    t_out = threading.Thread(target=_drain, args=(p.stdout, out_lines), daemon=True)
    t_err = threading.Thread(target=_drain, args=(p.stderr, err_lines), daemon=True)
    t_out.start()
    t_err.start()

    def send(line: str) -> bool:
        if not line.endswith("\n"):
            line += "\n"
        try:
            assert p.stdin is not None
            p.stdin.write(line)
            p.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError) as exc:
            LOG.warning("cec-client input pipe closed (%s); it likely exited early", exc)
            return False

    try:
        time.sleep(init_wait)  # bus scan + logical-address allocation

        if p.poll() is not None:
            LOG.error(
                "cec-client exited early (rc=%s) before any command was sent: %s",
                p.returncode, ("".join(err_lines) or "".join(out_lines)).strip(),
            )
            return

        if power_check:
            state = "unknown"
            for probe in range(2):  # a single 'pow' can be missed; try twice
                del out_lines[:]
                if not send(f"pow {device}"):
                    break
                time.sleep(3.0)
                state = parse_power_state("".join(out_lines) + "".join(err_lines))
                if state != "unknown":
                    break
                LOG.debug("power state query returned 'unknown' (probe %d/2)", probe + 1)
            if state == "unknown":
                LOG.error(
                    "TV at device %s did not report a power state -- it appears to have "
                    "gone offline (its HDMI-CEC controller has powered down). Skipping "
                    "these commands. Fix: disable the TV's deep sleep / eco / low-power "
                    "standby settings so it keeps CEC alive (see README). Set "
                    "'power_check: false' in the config to send commands anyway.",
                    device,
                )
                return
            LOG.info("TV power state: %s", state)

        attempts = retries + 1
        for i, cmd in enumerate(commands):
            line = build_stdin_for_command(cmd, device)
            for attempt in range(1, attempts + 1):
                if p.poll() is not None:
                    LOG.warning(
                        "cec-client exited early (rc=%s); aborting remaining commands", p.returncode
                    )
                    return
                LOG.info("Command %d/%d: %s (attempt %d/%d)", i + 1, len(commands), cmd, attempt, attempts)
                if not send(line):
                    return
                if attempt < attempts and retry_delay > 0:
                    LOG.debug("Sleeping %.2fs before retry", retry_delay)
                    time.sleep(retry_delay)

            if i < len(commands) - 1 and inter_delay > 0:
                LOG.debug("Sleeping %.2fs before next command", inter_delay)
                time.sleep(inter_delay)

        time.sleep(settle_wait)  # let the final frame be transmitted
    finally:
        try:
            if p.stdin is not None and not p.stdin.closed:
                p.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        t_out.join(timeout=2)
        t_err.join(timeout=2)
        combined_out = "".join(out_lines).strip()
        combined_err = "".join(err_lines).strip()
        LOG.info("cec-client exited: %s", p.returncode)
        if combined_out:
            LOG.info("cec-client output:\n%s", combined_out)
        if combined_err:
            LOG.info("cec-client stderr:\n%s", combined_err)


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
    power_check = bool(cfg.get("power_check", True))
    hdmi_port = cfg.get("hdmi_port")
    hdmi_port = str(hdmi_port) if hdmi_port not in (None, "") else None
    cec_log_level = int(cfg.get("cec_log_level", 1))

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

        def job_wrapper(sc=sc, device=device, cec_client_path=cec_client_path, dry_run=dry_run,
                        item_delay=item_delay, power_check=power_check,
                        hdmi_port=hdmi_port, cec_log_level=cec_log_level):
            LOG.info("Executing scheduled job '%s' at %s (commands=%s)", sc.name, datetime.now().astimezone(), sc.commands)
            try:
                run_cec_job(
                    cec_client_path,
                    sc.commands,
                    device,
                    inter_delay=item_delay,
                    retries=sc.retries,
                    retry_delay=sc.retry_delay,
                    power_check=power_check,
                    hdmi_port=hdmi_port,
                    cec_log_level=cec_log_level,
                    dry_run=dry_run,
                )
            except Exception:
                LOG.exception("Scheduled job '%s' failed", sc.name)
            else:
                LOG.info("Finished scheduled job '%s' at %s", sc.name, datetime.now().astimezone())

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
    scheduler = BackgroundScheduler(
        timezone=local_tz,
        job_defaults={
            # Still fire a job if we're up to 30 minutes late (e.g. the host was
            # briefly starved or the clock stepped via NTP just after the run time).
            "misfire_grace_time": 1800,
            # If several fire times were missed (host asleep/offline), run once.
            "coalesce": True,
        },
    )
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
