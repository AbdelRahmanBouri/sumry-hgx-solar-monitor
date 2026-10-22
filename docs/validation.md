# Validation and limits

This project supports one observed SUMRY HGX installation and a simulated inverter. Its purpose is to make local energy use visible, retain the raw evidence needed to investigate the protocol, and estimate battery charge and unused solar output. It also supports deliberate, audited configuration changes. It is not a general inverter controller or an independent energy meter.

## What the evidence supports

| Output | Evidence | Limit |
| --- | --- | --- |
| Decoded voltage, current and power | Captured frames in `tests/test_decode.py`, checksum tests, recorded front-panel comparisons in `decisions.md` | The repository contains a small capture fixture, not the full installation dataset or independent instrument calibration. |
| Configuration writes | Writable-field bounds, byte-mask checks, write authorization tests and simulated read-back | A matching read-back proves the reported bytes changed; it does not establish electrical suitability or the meaning of an unidentified register. |
| Utility use | Transfer-relay and power-balance inference | Not a metered utility-current channel; displayed power is a lower-bound estimate. |
| Battery charge, capacity and runtime | Current integration, full/rest anchors and synthetic regression cases | Dependent on the fixed 24 V AGM curve, current readings and anchor assumptions. Capacity is inferred, not an independently measured discharge-test result. |
| Solar potential | Sun position, irradiance transposition, weather and learned output envelopes | A planning estimate. Curtailment makes some sun elevations unobservable, and weather forecasts are not on-site irradiance measurements. |
| Wiring scenarios | A stored mismatch lookup and roof geometry | The original single-diode generator is absent. The lookup's accuracy cannot be reproduced here or generalized to another array. |
| Demo | The real transport, decoder, collector, database and web routes against `sim://` | Shared encoder/decoder tables establish integration consistency, not independent proof of the physical register map. All demo readings are synthetic. |

The numerical observations recorded in `decisions.md` describe the original installation. Where the underlying dataset is not included, those observations are historical evidence claims, not benchmark results reproducible from this checkout. Protocol attribution and the MIT licence remain part of the repository.

## Repeatable checks

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m coverage run --branch --source=solar_monitor -m unittest discover -v
.venv/bin/python -m coverage report
.venv/bin/ruff check .
node --test tests/js/*.test.js
```

The Python suite runs without a physical inverter. Node adds JavaScript syntax and chart checks. CI tests Python 3.11–3.13 and requires at least 95% combined line and branch coverage **for each** of `collector.py`, `change_policy.py`, `propose.py`, `serial_io.py`, `protocols/base.py` and `protocols/sumry_8851.py`. The report measures application code only and is retained as a CI artifact. Coverage shows which paths ran; assertions about results, audit records and prevented transmissions establish their expected behaviour.

The focused tests include every single-bit mutation of the captured status and configuration fixtures, valid-CRC structural errors, failed transmission/read-back, policy revocation during a read, competing claims and proposal commands that never open a port. CI also runs a separate demo job:

```sh
.venv/bin/python run.py --demo
# In another terminal, once /health is ready:
.venv/bin/python tools/check_demo.py
.venv/bin/python tools/screenshot.py --verify --all --login demo:demo-password --out /tmp/solar-shots
```

`check_demo.py` refuses a collector whose port is not `sim://`. It checks telemetry, history, estimates, login, write refusal, a verified simulated change and restoration of the original value, then switches writes off. Chromium renders phone and desktop views, requires populated demo data and charts with `--verify`, and fails on script exceptions, failed requests and HTTP errors for pages, scripts, styles and API calls. CI retains the screenshots and demo log. The browser uses a temporary profile.

To check distribution contents, install `build` and run `python -m build`. CI installs the wheel and runs `tools/check_package.py` from outside the checkout to exercise its simulator, collector, API, templates and static assets. The check rejects imports from the source tree. The documented `run.py` launcher is used from the source checkout.

## Operational boundaries

- The dashboard and telemetry API are public to anyone who can reach the service. Administration requires a session. The supplied proxy uses HTTP, so use it on a trusted private network; it is not an internet-facing deployment configuration.
- Session cookies use `HttpOnly` and strict same-site handling. Browser changes from an explicitly different origin are rejected, admin responses are not cached, and pages cannot be embedded in a frame. This does not protect credentials sent over an untrusted HTTP connection.
- Host controls, including reboot, service restart and Wi-Fi scanning or connection, are blocked when the collector uses `sim://`.
- Writes default to off. Disabling waits for any write already in progress; it cannot undo bytes already sent. Queued requests fail closed if permission or policy has been revoked, and requests older than twelve hours expire. Numeric requests must fit the register resolution; read-back verification cannot accept a different register step. Failed or unverified changes require investigation rather than an automatic retry.
- A valid status reply is required to refresh collector health. Corrupt or missing replies remain diagnostic records. An endless stream of serial bytes is bounded so the collector can return to its polling loop.
- Charge counting becomes unavailable after a gap longer than five minutes until a new anchor. Absorption holds cannot bridge missing telemetry longer than five minutes. Energy summaries cap gaps at five minutes; that is an interpolation convention, not evidence of what happened during the missing interval.
- Daily boundaries follow the server's timezone rules for each date, including 23-hour and 25-hour days. The dashboard preserves a full day at the supported ten-second minimum interval before client-side chart thinning.
- Weather is fetched from Open-Meteo for the configured coordinates. Local monitoring continues without a cloud account; the weather service is still an external dependency.

Further hardware validation needs captured frames, the exact firmware and panel settings, and independent measurements. The simulator and software tests cannot substitute for those observations.
