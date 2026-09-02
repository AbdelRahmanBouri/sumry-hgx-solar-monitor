from .sumry_8851 import Sumry8851ProbeProtocol


PROTOCOLS = {Sumry8851ProbeProtocol.name: Sumry8851ProbeProtocol}


def create_protocol(name: str):
    try:
        return PROTOCOLS[name]()
    except KeyError as exc:
        raise ValueError(f"Unknown protocol: {name}") from exc

