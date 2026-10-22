# Installation and operation

The commands below run from the repository root. For a hardware-free first look, use the [demo](../README.md#run-the-demo). This deployment is intended for a trusted private network.

## Install on a Raspberry Pi

Wiring first:

- The inverter's RJ45 COMM port is **not Ethernet**. Do not plug it into the Pi's Ethernet port or a network switch.
- Verify the port's RS-232 pinout and levels before connecting. Use a true USB-to-RS232 adapter (not a TTL UART), cross TX and RX as required, connect signal ground, and leave unused pins unconnected.
- Keep the inverter disconnected from mains and PV while assembling or changing the wiring, following its manual.

Then:

```sh
sudo apt update && sudo apt install -y python3-venv avahi-daemon nginx
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml        # then edit it
sudo usermod -aG dialout "$USER"           # then log out and in
```

Confirm the wiring before running the collector. `--listen-only` transmits nothing; without it the tool sends only the two allow-listed read queries.

```sh
.venv/bin/python diagnostic.py --listen-only --timeout 3
.venv/bin/python diagnostic.py --json-log captures/hgx.jsonl
```

Run it:

```sh
.venv/bin/python run.py --config config.toml          # serves with waitress
.venv/bin/python run.py --config config.toml --dev    # Flask's development server
.venv/bin/python run.py --no-collector                # web UI only, no serial polling
```

Open `http://<pi>:5000`. On first start the log names the `setup-token` file beside the database; visit `/admin/setup?token=<its contents>` to create the admin account.

For a permanent install, `deploy/solar-monitor.service` runs it under systemd (it assumes `/opt/solar-monitor`, a `solar-monitor` service account and `/var/lib/solar-monitor`; adjust the paths and service account for your installation), and `deploy/nginx-solar-monitor.conf` puts it on port 80 for every hostname the Pi answers to. After updating the code, restart the service: pages are cached by the running process.

## Configuration

`config.toml` is untracked; copy `config.example.toml`. A file named with `--config` must exist; with no `--config`, `config.toml` is used if present.

| Table | Keys |
| --- | --- |
| `[serial]` | `port` (`"auto"` picks the first USB adapter; `"sim://"` the simulator), `baudrate`, `timeout_seconds`, `poll_seconds`, `protocol`, `require_clock_sync` |
| `[web]` | `host`, `port`, `expose_network_info`, `secure_cookies` |
| `[storage]` | `database` (SQLite path; its directory also holds the secret and setup-token files) |
| `[site]` | `latitude`, `longitude`, `array_capacity_watts`, `performance_ratio`, `tilt_degrees`, `azimuth_degrees`, `dual_slope`, `split_strings`, `array_description`, `module_count`, `module_watts`, `wiring`, `vmp_volts`, `voc_volts`, `imp_amps`, `isc_amps`, `inverter_model`, `charge_limit_amps`, `battery_description`, `battery_nominal_volts`, `battery_capacity_ah` |

`require_clock_sync` (on by default) holds readings back until the clock has been set from the network, because a Pi without a real-time clock boots with a stale one; turn it off on a Pi with a battery-backed RTC that may have no internet.

Every `[site]` key is optional. Without a location and array capacity the estimator is off and the potential page says so. `performance_ratio` is the fallback when there are too few suitable readings. With enough readings, the estimator learns efficiency by sun-elevation band from recent observed output. `dual_slope` splits the array across both faces of a pitched roof; `split_strings` says each series string straddles both faces.

The dashboard needs no login, so `/api/system` leaves out the hostname, address and Wi-Fi network unless `expose_network_info` is set; the admin portal shows them either way. The polling rate, retention windows, estimator inputs, writes switch and auto-apply policy are changed in the admin portal, stored in SQLite, and take effect without a restart. The serial link itself is read from the config file at startup.

## API

<details>
<summary>Public endpoints</summary>

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/`, `/estimate` | dashboard, solar potential page |
| GET | `/api/status` | latest sample with the derived utility state, latest configuration, last raw exchange, collector state |
| POST | `/api/refresh` | one immediate poll, rate-limited to once per 5 seconds |
| GET | `/api/history?hours=N&fields=a,b&points=N` | samples for the last N hours (max 720), optionally only the named columns |
| GET | `/api/history/daily?days=N` | per-day energy, averages, peaks and minimum battery voltage |
| GET | `/api/history/faults?days=N` | rows with a non-empty faults field |
| GET | `/api/battery` | charge count, anchors, estimated capacity, runtime to cutoff, 14 days of health |
| GET | `/api/estimate?hours=N` | modelled output for the window plus six hours ahead, calibration, scenarios |
| GET | `/api/system` | Pi uptime, CPU temperature, throttling, eco mode (and network, if exposed) |
| GET | `/health` | process and collector health; 503 when the serial link has gone quiet |

</details>

<details>
<summary>Admin endpoints (session required)</summary>

`/admin/setup`, `/admin/login`, `/admin/pin-login` and `/admin/logout` manage the session.

| Method | Path | Does |
| --- | --- | --- |
| GET | `/admin` | the portal |
| GET | `/api/admin/overview` | serial link, storage counts, disk, system snapshot |
| GET, POST | `/api/admin/settings` | read or change runtime settings |
| POST | `/api/admin/serial/listen`, `/api/admin/serial/probe` | passive capture, or one allow-listed status query |
| GET | `/api/admin/frames?limit=N` | recent raw exchanges |
| POST | `/api/admin/data/cleanup` | run retention now |
| GET | `/api/admin/audit` | recent audit events |
| GET, POST | `/api/admin/security` | PIN status; set or disable the PIN |
| GET | `/api/admin/inverter` | writes switch, current configuration, writable fields, configuration history, proposals, policy |
| POST | `/api/admin/inverter/writes` | switch writes on or off |
| POST | `/api/admin/inverter/settings` | write changed fields and read them back |
| POST | `/api/admin/inverter/restore` | write an earlier configuration back |
| POST | `/api/admin/inverter/requests/<id>/approve`, `.../reject` | decide a proposal |
| POST | `/api/admin/inverter/auto-apply` | switch and edit the auto-apply policy |
| GET, POST | `/api/admin/system/eco` | read or set the CPU governor |
| GET, POST | `/api/admin/system/wifi` | scan, or connect to a network |
| POST | `/api/admin/system/restart-service`, `/api/admin/system/reboot`, `/api/admin/system/update-check` | Pi controls |
| GET | `/api/admin/export/frames.jsonl`, `/api/admin/export/samples.csv` | exports |

</details>

## Raspberry Pi controls

The portal's Raspberry Pi tab can switch the CPU governor, restart the service, reboot, and scan or join Wi-Fi networks. All of these run through one helper, `deploy/pi-control.sh`, which never evaluates its arguments as shell and accepts only a fixed set of actions. A Wi-Fi passphrase reaches it on standard input rather than as an argument, to keep it out of command-line arguments and sudo logs. To enable them:

1. Install the helper where the service account cannot change it: `sudo install -o root -g root -m 0755 deploy/pi-control.sh /usr/local/sbin/solar-monitor-pi-control`. The app uses that copy whenever it exists; re-run the command after an update. The sudoers rule grants root to whatever the file contains, which is why it must not be a file the service account can rewrite, such as the copy in a checkout under its home directory.
2. Install the sudoers rule, adjusting the user: `sudo cp deploy/solar-monitor-sudoers.example /etc/sudoers.d/solar-monitor && sudo chmod 440 /etc/sudoers.d/solar-monitor && sudo visudo -c`.
3. Make sure the systemd unit does not set `NoNewPrivileges=true`, which blocks `sudo` whatever sudoers says. The shipped unit leaves it off.
4. Wi-Fi actions need NetworkManager (`nmcli`), the default on Raspberry Pi OS Bookworm and later.

Without this setup the buttons return a clear error instead of hanging.

## Browser and authentication protections

The content security policy allows scripts, styles, API requests and the app manifest from this server only. Inline scripts, inline style attributes, embedded frames and plugins are blocked. The browser verifier fails on CSP violations as well as request and script errors.

The admin location button can request browser permission to fill the site coordinates on HTTPS or localhost. Plain HTTP on a LAN may not offer geolocation; enter coordinates manually there. Camera and microphone access are blocked. Filling coordinates does not save them: review the form and choose Save estimate settings.

Sessions use HttpOnly, SameSite=Strict, a host-only cookie and an eight-hour lifetime that ordinary page requests do not extend. Direct HTTPS sets Secure automatically. Behind a TLS-terminating proxy, set `[web] secure_cookies = true` and serve the browser-facing site over HTTPS; HTTP login cookies will then be refused by browsers. The application does not trust forwarded headers. HSTS is sent on direct HTTPS; configure it at your HTTPS proxy when TLS terminates there. The supplied HTTP LAN setup and localhost demo remain usable without Secure.

Authentication attempts share SQLite rate-limit budgets across threads and restarts: five per username, twenty per source address, and one hundred overall in a fifteen-minute window. Password success resets the account budget; source and global budgets remain. PIN and setup requests share those source and global limits. Exhaustion returns HTTP 429 with Retry-After; it does not extend the window. PIN failures still require the full password after three failures. A reverse proxy's address is treated as one source, so size any additional proxy limits for your deployment. The global budget bounds stored keys but can temporarily deny legitimate logins during an attack.

CI audits the resolved runtime dependency set against published advisories and saves the report. A clean audit means no known advisory was reported for that resolution at that time; it does not establish that the application has no vulnerabilities.
