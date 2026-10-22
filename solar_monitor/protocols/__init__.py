from .sumry_8851 import Sumry8851Protocol

PROTOCOLS = {Sumry8851Protocol.name: Sumry8851Protocol}

# The settings table and existing config files name the protocol as it was
# called before it could write; both names select the same class.
LEGACY_NAMES = {"sumry_8851_probe": Sumry8851Protocol.name}


def create_protocol(name: str) -> Sumry8851Protocol:
    try:
        return PROTOCOLS[LEGACY_NAMES.get(name, name)]()
    except KeyError as exc:
        raise ValueError(f"Unknown protocol: {name}") from exc
