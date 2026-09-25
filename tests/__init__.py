import logging

import serial

# The suite provokes failures on purpose -- dead serial links, stale clocks --
# and the collector logs every one. A handler on the root logger stops
# logging's last-resort handler printing them over the test report;
# assertLogs still captures each record it is waiting for.
logging.getLogger().addHandler(logging.NullHandler())


# The suite runs on the same Pi that is polling the inverter, so a test that
# reaches a real serial device talks to the live hardware and collides with
# the live collector's poll -- one did, logging "multiple access on port" in
# the live service on 2026-09-25. Opening a real device from a test is
# therefore an error. sim:// and the other pyserial URL handlers are
# separate classes and unaffected.
def _no_real_ports(port: serial.Serial) -> None:
    raise serial.SerialException(f"the test suite may not open a real serial device ({port.port})")


serial.Serial.open = _no_real_ports
