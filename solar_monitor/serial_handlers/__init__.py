"""pyserial URL handlers. serial_io registers this package, so a port named
`sim://` opens the simulated inverter (solar_monitor/simulator.py) through
the same `serial.serial_for_url` call that opens a real adapter."""
