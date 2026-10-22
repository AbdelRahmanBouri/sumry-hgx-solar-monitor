# Making a household solar inverter easier to understand

I wanted to see where solar power was going, keep enough history to investigate problems, and make configuration changes traceable. This project connects a Raspberry Pi to one SUMRY HGX 4 kW, 24 V inverter and presents its readings in a local web dashboard.

The useful engineering problem was deciding what to trust: a valid reply can still contain an unidentified register, and a convincing chart can still be an estimate.

## Hardware and protocol constraints

The inverter's RJ45 communication port carries RS-232, not Ethernet. The software uses a serial adapter and supports one protocol implementation. It checks frame length, command, address, declared payload size and CRC16/MODBUS before decoding the 158-byte status and 104-byte configuration replies.

The community register maps were a starting point. Some meanings remain unconfirmed for this firmware, including the battery-type bits. Those bits are exposed as unknown and can only be changed back to zero for restoration. A matching read-back confirms reported bytes, not their physical meaning.

Configuration changes rewrite a whole 94-byte payload. That makes preserving unrelated settings essential. The [protocol boundary tests](../tests/test_protocol_boundaries.py) exercise corrupted captures, valid-checksum structural errors and refused writes before a serial port opens.

## Design decisions and safeguards

**Keep the evidence locally.** A Python collector stores decoded readings, raw exchanges, configuration snapshots and audit events in SQLite. Flask and Waitress serve plain HTML, CSS and JavaScript. Raw captures let me revisit a decoding assumption without relying only on what the dashboard displayed.

**Put write checks at the collector.** Writes start disabled. An admin can request a change directly or approve a proposal; automatic approval needs an explicit policy. The collector reads the current configuration, changes only permitted fields, checks authorization again and reads back the result. Polling and writes share a lock within the collector process. Failed or unverified requests are recorded and are not automatically retried; pending and approved proposals expire after twelve hours. [Failure tests](../tests/test_write_failures.py) cover transmission loss, missing read-back and policy revocation.

**Treat gaps and estimates honestly.** Daily energy integrates reported power, caps gaps at five minutes and splits intervals at local midnight, including daylight-saving transitions. Battery charge counting becomes unavailable after a longer gap until another anchor. Battery capacity, runtime, utility draw and solar potential are estimates with stated assumptions. The solar comparison uses absolute differences so overestimates and underestimates cannot cancel into a perfect score. Its input can overlap calibration data, so it is not independent validation.

**Make the software testable without hardware.** The demo implements `sim://` below the serial transport, exercising the same decoder, collector and write checks. It generates synthetic history in a separate database and disables host controls. Browser protections include a same-origin content security policy, session cookie flags and persistent login-attempt limits. Keyboard navigation and chart tables make the main views usable beyond pointer interaction.

## Verified results

The [hardening CI run](https://github.com/AbdelRahmanBouri/sumry-hgx-solar-monitor/actions/runs/39798335008) passed on the revision underlying this page:

- 315 Python tests across Python 3.11–3.13, plus 20 JavaScript tests.
- Strict type checking across 22 application and launcher files. Each of six collector, serial, protocol and write-control modules exceeded the 95% combined line-and-branch coverage gate.
- Installed-wheel and simulated write/refusal/restore checks, plus six Chromium views with keyboard checks and no reported axe violations. Dependency audits reported no known vulnerabilities for the versions resolved in that run.

These results establish tested software behavior. The simulator shares encoder/decoder tables, so it cannot independently validate the register map. The full historical hardware datasets, independent metering and mismatch-model generator are absent. Automated accessibility checks do not establish full WCAG conformance. The supplied deployment targets a trusted private network, and this review did not change the live inverter.

[Run the demo](../README.md#run-the-demo) · [Validation commands and limits](validation.md) · [Detailed decisions](decisions.md)
