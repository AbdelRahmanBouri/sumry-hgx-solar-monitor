# Demo screenshots

These PNGs were captured from the running `sim://` demo on 22 October 2026 UTC. They show synthetic telemetry and estimates, not readings from a physical installation. The application source was at commit `b13d69bdabecf640f830dd64399a9bff5f3ff297`. They predate the later contrast, keyboard and chart-table changes; CI captures the current six views as a workflow artifact.

| File | Viewport | Scheme |
| --- | --- | --- |
| dashboard-phone.png | 390 × 844, scale 1 | Light |
| dashboard-desktop-dark.png | 1280 × 900, scale 1 | Dark |
| estimate-desktop.png | 1280 × 900, scale 1 | Light |

Captured with headless Google Chrome through `tools/screenshot.py`. The check waited for populated data and chart state and reported no script or request failures. The screenshots are direct browser output; they have not been retouched. The demo follows the clock, so a later capture will show different values.

From the repository root, with the demo running in another terminal:

```sh
.venv/bin/python run.py --demo --host 127.0.0.1 --port 5058
```

Then:

```sh
.venv/bin/python -O tools/check_demo.py --base http://127.0.0.1:5058
.venv/bin/python tools/screenshot.py --base http://127.0.0.1:5058 \
  --verify --scale 1 --wait-ms 1000 --out docs/screenshots
```

The API check exercises a simulated write and restores the original value, then turns writes off. It refuses an instance whose configured serial port is not `sim://`.
