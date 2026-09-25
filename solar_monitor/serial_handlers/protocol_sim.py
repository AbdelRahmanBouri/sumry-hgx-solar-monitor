"""The `sim://` port: a pyserial port whose far end is the simulator.

Every frame written to it is answered by the installed SimulatedInverter,
and the reply waits in the input buffer exactly as bytes from a USB adapter
would, so the collector's transport code runs unchanged.
"""
from __future__ import annotations

from serial.serialutil import PortNotOpenError, SerialBase, SerialException


class Serial(SerialBase):
    """Named `Serial` because that is the class pyserial looks up."""

    def open(self) -> None:
        from .. import simulator  # deferred: the simulator is only needed once a port opens

        if self._port is None:
            raise SerialException("Port must be configured before it can be used.")
        if self.is_open:
            raise SerialException("Port is already open.")
        self._inverter = simulator.installed()
        self._pending = bytearray()
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def _reconfigure_port(self, *args, **kwargs) -> None:
        """Line settings mean nothing to a simulator."""

    def _require_open(self) -> None:
        if not self.is_open:
            raise PortNotOpenError()

    @property
    def in_waiting(self) -> int:
        self._require_open()
        return len(self._pending)

    def read(self, size: int = 1) -> bytes:
        self._require_open()
        data = bytes(self._pending[:size])
        del self._pending[:size]
        return data

    def write(self, data) -> int:
        self._require_open()
        request = bytes(data)
        self._pending.extend(self._inverter.respond(request))
        return len(request)

    def flush(self) -> None:
        self._require_open()

    def reset_input_buffer(self) -> None:
        self._require_open()
        self._pending.clear()

    def reset_output_buffer(self) -> None:
        self._require_open()

    # Modem lines: nothing is wired to them.
    def _update_break_state(self) -> None:
        pass

    def _update_rts_state(self) -> None:
        pass

    def _update_dtr_state(self) -> None:
        pass

    @property
    def cts(self) -> bool:
        return True

    @property
    def dsr(self) -> bool:
        return True

    @property
    def ri(self) -> bool:
        return False

    @property
    def cd(self) -> bool:
        return True
