# SUMRY HGX Solar Monitor

A lightweight, local-first Raspberry Pi monitor for a SUMRY HGX 4 kW 24 V/120 V inverter. This first milestone is intentionally **read-only**. It captures raw serial replies and provides the dashboard/storage structure without guessing at unconfirmed HGX field offsets.

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

- `/api/status` — latest decoded sample and latest raw diagnostic
- `/api/history` — decoded samples from the last 24 hours
- `/health` — process/read-only health marker

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
