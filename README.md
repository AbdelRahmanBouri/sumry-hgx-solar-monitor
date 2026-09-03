# SUMRY HGX Solar Monitor

A lightweight, local-first Raspberry Pi monitor for a SUMRY HGX 4 kW 24 V/120 V inverter. It is intentionally **read-only**. It captures raw serial replies and decodes the validated status frame into dashboard metrics.

## Safety and connection notes

- The only transmitted frame is the supplied candidate status query: `88 51 00 03 00 00 00 00 4D 08`.
- The serial transport enforces an allow-list. Arbitrary frames and all unimplemented commands are rejected.
- Begin with `--listen-only`, then use the candidate query after confirming the adapter wiring.
- An RJ45 connector here is **not Ethernet**. Do not plug the inverter COMM port into the Pi's Ethernet port or a network switch.
- Verify the inverter's RJ45 RS-232 pinout and signal levels before connecting. Use a true USB-to-RS232 adapter (not TTL UART), connect TX/RX crossed as required, and connect signal ground. Do not connect unused voltage pins.
- Keep the inverter disconnected from mains/PV while assembling or changing the breakout wiring, following its manual and normal electrical-safety practice.

## Raspberry Pi setup

```sh
sudo apt update
sudo apt install -y python3-venv avahi-daemon nginx
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml
```

Add the account running the monitor to the serial-device group, then log out/in:

```sh
sudo usermod -aG dialout "$USER"
```

Raspberry Pi OS normally publishes its hostname through Avahi. Set the Pi hostname to `solar-monitor`:

```sh
sudo hostnamectl set-hostname solar-monitor
sudo systemctl enable --now avahi-daemon
```

For the requested no-port URL, install `deploy/nginx-solar-monitor.conf` as `/etc/nginx/sites-enabled/solar-monitor`, remove the default site if it conflicts, test the Nginx configuration, and reload Nginx. It forwards `http://solar-monitor.local` to the app on localhost port 5000.

The included systemd example assumes installation at `/opt/solar-monitor`, a `solar-monitor` service account, and writable storage at `/var/lib/solar-monitor`; adjust those paths during deployment.

## Capture the first frames

List detected adapters without transmitting by running the utility in listen-only mode:

```sh
.venv/bin/python diagnostic.py --listen-only --timeout 3
```

Then issue the single allow-listed 9600 8N1 status candidate and save a JSON Lines capture:

```sh
.venv/bin/python diagnostic.py --json-log captures/hgx.jsonl
```

You can select a stable device path explicitly:

```sh
.venv/bin/python diagnostic.py --port /dev/serial/by-id/YOUR_ADAPTER
```

The output includes TX HEX, RX HEX, printable ASCII, timing, port, baud, and errors. Share the capture plus the exact inverter model/firmware label to implement and validate the decoder. A baud can be explicitly selected with `--baud`, but do not probe arbitrary rates until the protocol is better established.

## Run the dashboard

```sh
.venv/bin/python run.py --config config.toml
```

Open `http://solar-monitor.local:5000` during direct development, or `http://solar-monitor.local` with the included Nginx configuration. The collector polls no faster than every 30 seconds, stores every raw exchange, and will begin storing metric samples once a decoder returns confirmed fields. Run only the web UI with `--no-collector`.

Endpoints:

- `/api/status` — latest decoded sample, raw diagnostic, and collector state
- `/api/refresh` — POST to trigger an immediate poll (rate-limited to once per 5 seconds)
- `/api/history?hours=N` — decoded samples for the last N hours (default 24, capped at 30 days)
- `/api/history/daily?days=N` — per-day averages, for the dashboard's 7/30-day totals
- `/api/history/faults` — recent fault transitions
- `/api/system` — Raspberry Pi uptime, network, CPU temperature, and eco-mode status
- `/health` — process, read-only, and collector health

## Raspberry Pi controls in the admin portal

The admin portal's "Raspberry Pi" tab can toggle CPU eco mode, restart the
collector service, reboot the Pi, and scan/connect to Wi-Fi networks. These
all run as root through a single scoped helper script, `deploy/pi-control.sh`,
which never evaluates its arguments as shell and only accepts a small,
validated set of actions.

To enable it:

1. Install the sudoers rule (adjust the username and path for your
   installation — see the comments in the file):
   ```sh
   sudo cp deploy/solar-monitor-sudoers.example /etc/sudoers.d/solar-monitor
   sudo chmod 440 /etc/sudoers.d/solar-monitor
   sudo visudo -c
   ```
2. Make sure the systemd unit does **not** set `NoNewPrivileges=true` — that
   setting blocks `sudo` unconditionally, independent of any sudoers rule.
   `deploy/solar-monitor.service` already reflects this; if you deployed
   before this change, edit your installed unit file, then:
   ```sh
   sudo systemctl daemon-reload
   sudo systemctl restart solar-monitor
   ```
3. Wi-Fi scanning/connecting requires NetworkManager (`nmcli`); on Raspberry
   Pi OS Bookworm and later this is the default.

Without this setup, the buttons still work but return a clear error instead
of hanging or silently failing.

## Project layout

```text
diagnostic.py                  serial discovery and raw capture tool
run.py                         web server plus background collector
solar_monitor/protocols/       isolated query definitions and decoder
solar_monitor/serial_io.py     guarded 9600 8N1 transport
solar_monitor/db.py            SQLite raw frames and metric history
solar_monitor/web.py           Flask routes
solar_monitor/templates/       mobile dashboard markup
solar_monitor/static/          vanilla CSS/JavaScript and chart
deploy/                        deployment examples
tests/                         safety and API tests
```

## Tests

```sh
.venv/bin/python -m unittest discover -v
```
