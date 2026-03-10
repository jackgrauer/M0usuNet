"""m0usunet exceptions."""


class M0usuNetError(Exception):
    """Base exception."""


class RelayError(M0usuNetError):
    """Relay subprocess failed."""
