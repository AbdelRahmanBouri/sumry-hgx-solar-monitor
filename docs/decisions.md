# Decisions

Each entry states a rule the code follows and the evidence that put it there. Dates identify the observation on this installation that settled the matter. Sources are the module docstrings, the comments beside the code, and the commit history.

## Frames are validated by CRC16/MODBUS before anything is decoded

**Rule.** A reply is decoded only if its trailing two bytes equal the CRC16/MODBUS of everything before them; otherwise the raw bytes are stored with a note and no metrics are produced.

The two real HGX status replies captured on 2026-09-02 both passed their trailing checksum, which is what confirmed they were intact 158-byte frames rather than a partial read. The same check is applied to the 104-byte configuration frame and to every outbound write. Field offsets were then accepted on internal consistency: load watts over output volts times amps gives a power factor near 0.92 on both captures, the battery reads about 27 V for a 24 V bank, and the output is 120 V at 59.9 Hz, matching the inverter's rating. A frame that fails the checksum is kept in `raw_frames` for diagnosis and counts as a diagnostic frame for retention.

## Offset 76 is not utility current, so utility use is derived

**Rule.** The status frame's word at offset 76 is not decoded, and utility use is inferred from the transfer relay and the power balance instead.

The community register map labels offset 76 as utility current and this project published it under that name until 2026-09-16. Across 4,489 frames with mains present it read exactly 0.00 through two nights while the utility was carrying 200 to 400 W of load on its own, with the battery flat and PV at zero, and rose to 0.4 to 0.9 A at midday when the panels were carrying everything. It correlates -0.48 with the power balance and +0.69 with PV watts. No other word in the frame tracks utility throughput. The field was removed rather than relabelled; rows written before that date keep the column in SQLite for diagnostics, but it is out of the write allow-list and hidden from every endpoint.

Two readings answer the question anyway (`solar_monitor/utility.py`). When the transfer relay closes, the inverter's own output stage, `output_voltage * output_current`, collapses to near zero while `ac_load_watts` keeps reporting the real load. Across 2,316 grid-present samples that ratio was cleanly bimodal, either under 20% of the load or over 100% of it, and across 6,147 grid-absent samples it never once fell below 100%. So a ratio under 0.5 means bypass, with no false positives in two weeks of data. The draw is sized from `load + battery charging - PV`, and the current is that power over the measured utility voltage. It is in-phase current, so the dashboard shows it as a lower bound rather than a meter reading.

## The battery-type bits are disproven, so battery type is not writable

**Rule.** Byte 8 bits 3 and 4 of the configuration frame are reported as `unknown_flag_byte8_bits34`, never as a battery type, and the admin form has no battery-type control.

The community structure labels those bits `battery_type`, and this project believed it. On 2026-09-05 the inverter's own panel (menu 05) read AGM while the bits decoded "User-defined". The mapping is wrong for this firmware. That also explained the earlier rejections of bulk and float voltage writes: the type never became User-defined, so the charger kept the AGM profile locked and refused custom charge voltages. A write on 2026-09-04 that reported "verified" had in fact set an unidentified flag, and the read-back could not catch it because it reads through the same offsets it writes to. Read-back proves a byte changed; it never proves the byte means what the table says. The bits stay writable only so the value written on 2026-09-04 can be restored to zero.

The rest of the block has been checked against the panel since. A reading of every front-panel menu on 2026-09-17 matched the decoded configuration captured over the same hours on every setting the panel shows, 24 decoded fields in all, among them the two earlier writes that did land: low-voltage cutoff 22.0 V (menu 29) and overload auto-restart (menu 06). The one disagreement was register 82, below.

## Register 82 is not an equalization switch

**Rule.** The configuration word at offset 82 is reported raw as `unknown_u16_offset82` and is not writable.

The community structure labels it the equalization on/off flag, and it was decoded and writable as a boolean until 2026-09-17. On that day's panel reading, menu 33 (equalization) read OFF while offset 82 held 4, a value no boolean takes, and menus 34 to 37 matched offsets 90, 92, 94 and 96 exactly. The word had moved from 0 to 4 at 19:58:57 on 2026-09-16, the moment the battery type changed, not when equalization did. Writing it as a boolean would have put a 0 or 1 into a register that means something else, so it left the writable table.

## Readings from an untrusted clock are discarded

**Rule.** A decoded reading is not stored if systemd-timesyncd has not yet set the clock, or if it is stamped earlier than a reading already stored. The raw frame is kept either way.

A Raspberry Pi has no real-time clock. On a cold boot it restores a stale time until NTP syncs. On 2026-09-05 that stored two genuine readings nearly ten hours in the past, showing 599 W of sun at midnight. The collector now checks the system's own sync flag at `/run/systemd/timesync/synchronized` and refuses any timestamp that goes backwards. Nothing is lost for diagnosis, because the raw exchange is still written.

## A failed exchange counts as a failure

**Rule.** The collector's failure count includes every exchange that returned an error, and the state carries the consecutive streak and when it started.

Until 2026-09-05 only unhandled exceptions were counted. A serial exchange that times out or hits an I/O error returns an `Exchange` with an error string rather than raising, so an adapter that stalled for 425 consecutive polls still reported `failures=0`. Nothing watching could have alarmed. The streak and its start time are what a watchdog needs, and `/health` exposes them.

## A full anchor requires a recent absorption

**Rule.** The charge counter re-anchors to 100% only when the bank holds at or above 26.9 V with under 5 A of charge current for ten minutes, and a real absorption (voltage within 0.35 V of the bulk setpoint) happened within the last six hours.

Float voltage alone used to fire the full anchor. On a fading afternoon the current drops under the tail threshold because the sun ran out, not because the bank stopped accepting, and one of this installation's three anchors on 2026-09-04 was doing exactly that. Without the recency test a bank idling at float anchors at 100% purely because the day ended.

## A rest anchor is C/50 with no charge current

**Rule.** The counter re-anchors from the open-circuit voltage curve when the current has stayed between -C/50 and +1 A for thirty minutes. Terminal voltage is corrected by 3 milliohms of internal resistance first.

A true zero-current rest never happens on a lived-in system. This bank's quietest overnight stretch sits at 3 to 4 A and never spends thirty minutes under 1.5 A, so a fixed low threshold would have meant the capacity was never measured at all. C/50 is light enough that terminal voltage is within millivolts of open circuit once polarisation settles, and it scales with the bank. The charge-current limit matters as much: 26.98 V at +3 A is the charger holding the bank up, and reading the chemistry curve off it returned a confident, wrong, 100%. A depleted bank at float would pull many amps, so a trickle under 1 A does mean the charger has nothing left to push.

## A low absorption setpoint makes measured capacity a lower bound

**Rule.** When the bulk setpoint is more than 0.1 V below 28.8 V, the state carries `charger_limited` and every capacity measurement is labelled a lower bound.

Both genuine absorptions on 2026-09-04 completed at 28.18 V where the chemistry wants 28.8 V for this bank. The charger's "full" is therefore perhaps 85 to 95%. Measured capacity is the charge taken out divided by the share of the bank it emptied, so an anchor that is short of full reads light: by roughly a quarter at a 90% anchor. The dashboard shows the figure with the reason rather than presenting it as a measurement. Raising the setpoint will raise it.

## Efficiency is learned per sun-elevation band as an envelope

**Rule.** For each 10-degree band of sun elevation, the model's level is the 90th percentile of measured PV over the clear-sky model across the last seven days, using only readings taken with the battery below 26.9 V, and requiring at least 20 samples per band and 60 in total.

A fixed performance ratio was wrong in both directions. Against unconstrained readings the array delivered 0.70 of the clear-sky model at 14 degrees of sun and 0.92 at 45, so a single ratio sat under real output exactly when the bank was accepting charge, which is the case the potential page exists to compare. Heat, glass reflection at low sun, haze and the horizon all vary with sun height, so the level is learned per band. Absorption-phase readings are excluded even when the bank still takes 15 A; they sat 12% under the free ones. A high percentile rather than a mean makes the line an envelope of demonstrated output, so passing clouds fall below it instead of dragging it down. Learned from the live database on 2026-09-04: 0.81, 0.88 and 0.85 for the 10 to 40 degree bands, and the line then ran 0 to 9% above unconstrained output rather than 13% below.

A steep single-plane geometry fitted to the logged data is an artefact and must not be used. Battery-full curtailment removes every midday sample, leaving only morning and evening readings, and low-sun-only data always prefers a steep tilt. The geometry is therefore set from the roof itself and only the level is learned.

## A symmetric gable collects about the same as a flat array

**Rule.** With `dual_slope` set, the array is modelled as half on each face of the roof, and the exact tilt is treated as a low-stakes assumption.

Two faces pointing opposite ways very nearly cancel. A symmetric gable collects within 1 to 2% of a flat array of the same size whatever the pitch and whichever way the ridge runs. This installation's faces run north and south (azimuths 5 and 185), read off the roofline in site photographs rather than from the data, which means four of the eight modules face north. Clearing tilt and azimuth falls back to the horizontal plane rather than inventing an angle.

## Series strings that straddle both faces lose output

**Rule.** With `split_strings` set, the combined irradiance of the two faces is multiplied by a mismatch factor keyed on the weaker face's share of the stronger one's irradiance.

Each of this array's 4-module series strings carries two modules on each face. Series modules must share one current, so the shaded pair pins what the sunlit pair may deliver. The factor comes from a single-diode model of the 96-cell modules wired 4S2P: the loss is worst near a 0.45 ratio and recovers below it, where the bypass diodes give up on the weak pair entirely, and it depends only on the ratio, not the absolute irradiance (verified to within 0.1%). It costs a few percent in summer and about a third at the winter solstice. Re-landing each string on one face is a wiring change and would remove the loss outright; the potential page quantifies it. The learned efficiency bands already contain whatever the straddling costs, so the live line applies the geometry only and the mismatch factor is used for the fallback ratio and the scenario comparison.

## Writes are whole-block rewrites, gated three ways

**Rule.** A write frame is the inverter's own last configuration reply with command `0x0010` in place of `0x0003`, the requested fields changed, and a fresh checksum. It passes the auto-apply policy or an admin's approval, the writable-field table, and `assert_write_frame` before the port is opened.

The HGX has no per-register write. Command `0x0010` takes the entire 94-byte block, so the only safe frame is the block the inverter just sent. `build_config_write` refuses a reply that is not a valid configuration read, an unknown field, or a value outside the inverter's own menu range. `assert_write_frame` then compares the frame to the reply it was built from and rejects any byte that differs outside the writable masks, any wrong length or command, and any bad checksum. The collector holds the port lock through read, write and read-back, and reports each requested value as verified only when the second read carries it. Proposals from tooling are queued rather than written, because the collector is the one process that owns the port; a proposal is released by an admin or by a policy that can only narrow what the table already permits, and a pending proposal expires after twelve hours so it cannot be approved later against a baseline that has moved.

## Raw-frame retention keys on whether a frame decoded

**Rule.** A raw frame is routine, kept for `raw_success_days`, only when it came back without an error and decoded. Everything else is a diagnostic kept for `raw_diagnostic_days`: serial errors, CRC failures, replies that would not decode, configuration writes and passive captures. The decision is a column set from the decoder's verdict, not a reading of the note.

Until 2026-09-25 the classifier searched each decode note for words such as "fault" and "not decoded". Every good status note contains "operating_state/faults ... not yet identified" and every good configuration note "battery type is not decoded", so no frame ever counted as routine: the live database held 24,781 successful frames older than a week. A write's note, "configuration write: [...]", contains none of the words, so the raw frames of all six configuration writes were purged after a week while everything else was kept. The test that let this through used a made-up note; the tests now use the decoder's own. Rows from before the column existed are classified by the one prefix every decoded note has always had, "CRC-validated".

## History is thinned with M4 to preserve peaks

**Rule.** The chart thins rows to its own pixel width by keeping, for each column, the rows holding the minimum and maximum of every plotted key plus the first and last row it covers.

A stride ("every nth row") drops peaks at random, so a 30-day view could disagree with the summary tiles by hundreds of watts. `ChartCore.downsample` implements the M4 method instead; rows come back in time order and unchanged. The API applies its own even sampling to cap the response size (`history_max_points`, default 1,500) while always keeping the first and newest reading; the complete series stays in SQLite and in the CSV export. Series are drawn as straight segments between readings. The earlier smoothed line passed each reading as a Bezier control point, which on this system's own history clipped 38 samples by more than 20 W and understated peaks by up to 10%.

Ranges longer than a day are drawn as energy per day from `/api/history/daily`, whose peaks and lows are taken over every reading. They used to plot each day's average power, and the peak tile then reported the largest average: 388 W for a simulated week that had peaked at 1.9 kW.

## The simulator sits below the serial transport

**Rule.** The demo's inverter is a pyserial URL handler: a port named `sim://` opens `solar_monitor/serial_handlers/protocol_sim.py`, and every port is opened with `serial.serial_for_url`. Everything above the port is the production code.

A stand-in at the collector or API level would have shown the pages but not the part of the project that took the work, and it would have drifted from it. Below the transport, the simulator is held to the same contract as the inverter: it answers only the two allow-listed queries, builds its frames from real captures through the decoder's own `STATUS_FIELDS` table so the two cannot disagree about where a reading lives, and applies a configuration write only if `assert_write_frame` accepts it. The tests run the real collector and write path against it.

## The dashboard has no build step, and static checks stand in for a browser

**Rule.** Pages are served as plain `<script>` tags with no bundler, no `node_modules` and no lockfile. The test suite checks the scripts statically, and uses Node only when it happens to be present.

The Pi has no browser and no JavaScript engine, so a syntax error or a script loaded in the wrong order fails silently in production and nowhere else. `tests/test_templates.py` scrapes every `#id` a script resolves at load time and requires the template to supply it, because a missing hook throws a TypeError and the whole page stops updating. `tests/test_static_assets.py` checks bracket balance with a lexer that understands strings, template literals, comments and regex literals, verifies that `chart-core.js` loads before its consumers and `theme.js` before the stylesheets, and asserts that no template points at a retired asset. When Node is available the suite additionally parses every script with `node --check` and runs the charting unit tests, which use Node's built-in runner; when it is absent those tests skip. `chart-core.js` carries a two-line CommonJS footer so the browser global and the test runner load the same file. To see the pages rather than reason about them, `tools/screenshot.py` drives headless Chromium over the DevTools protocol, standard library only, and reports any uncaught page error.
