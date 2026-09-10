# CEC Scheduler 🔧

Schedule CEC commands (via the `cec-client` binary) to run at specific times of day.

---

> [!CAUTION]
> ## ⚠️ DISABLE THE TV's DEEP SLEEP / ECO POWER SETTINGS ⚠️
>
> **Many TVs cut power to their HDMI-CEC controller after a short idle period
> (e.g. the ONN TV drops CEC roughly 15 minutes after going to standby). Once
> that happens the TV is completely deaf to CEC and _nothing this scheduler
> sends can wake it_ — the scheduled `on` command will silently do nothing.**
>
> Before relying on this scheduler, go into the TV's settings and turn **off**
> anything along these lines:
>
> - **"Eco" / "Energy saving" / "Power saving" standby mode**
> - **"Deep sleep" / "Deep standby"**
> - **"Quick start" / "Fast start" set to its low-power option** (you usually
>   want quick-start *enabled*, which keeps CEC alive)
> - Any "auto power off" / "sleep after N minutes" timer that fully powers the set down
> - Vendor-branded CEC names are fine to leave **on**: *Anynet+* (Samsung),
>   *Bravia Sync* (Sony), *SimpLink* (LG), *CEC* / *HDMI Control* (others)
>
> **Quick check:** put the TV in standby, wait ~20 minutes, then run
> `echo 'pow 0' | cec-client -s -d 1` on the Pi. If it reports a power state,
> CEC is still alive and the scheduler will work. If it times out or returns
> `unknown`, the TV has powered down its CEC controller and you must fix the
> settings above.

---

## Installer script & systemd unit 🔧

An installer script `install.sh` and an example systemd unit `cec-scheduler.service` are included.

Quick install (installs to `/opt/cec-scheduler` by default):

```bash
# run as root
sudo bash install.sh --install-dir /opt/cec-scheduler --service-user pi
```

Options:
- `--install-dir DIR` (default: `/opt/cec-scheduler`) — where files are copied
- `--service-user USER` (default: current user) — which user the unit will run as
- `--config FILE` — path to an existing YAML config to install; otherwise the example config is copied
- `--no-start` — install and enable the unit but do not start it immediately

The installer does:
- copies `cec_scheduler.py` and the config to the install dir
- installs the systemd unit at `/etc/systemd/system/cec-scheduler.service` from the included template
- runs `systemctl daemon-reload` and `systemctl enable --now cec-scheduler.service` (unless `--no-start`)

Command delay:
- You can add a per-item `command_delay` setting (seconds) which is the delay between running multiple commands in a single schedule entry. The global default is `command_delay: 1.0` (seconds) which can be set at the top-level of your YAML file. This helps ensure devices have time to process one command before the next is sent.

You can inspect the example unit in `cec-scheduler.service` — replace `{{INSTALL_DIR}}` and `{{SERVICE_USER}}` if you edit it manually.

Logs:
- View logs with: `sudo journalctl -u cec-scheduler -f`

---

## Configuration format
See `cec_schedule.example.yaml` for examples. Key fields:

- `device` — logical device address (e.g. `0`)
- `cec_client_path` — optional path to the `cec-client` binary
- `commands` — list of commands with `time`, `command`, optional `days` and optional `name`

Time format: `HH:MM` or `HH:MM:SS`.

Note: scheduled times in the YAML are interpreted in the system's local timezone (not UTC).

Days: `mon,tue,wed,thu,fri,sat,sun` — omit to run every day.

Commands: either a single `command` string (legacy) or `commands` as a list of strings. Common values are `on` and `standby`. You may also pass raw `tx` pairs like `tx 40:44:41:00`. Example: `commands: ['standby', 'tx 40:44:41:00']` will run both commands in order.

---


