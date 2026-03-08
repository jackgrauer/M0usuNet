"""m0usunet exceptions."""


class M0usuNetError(Exception):
    """Base exception."""


class RelayError(M0usuNetError):
    """Relay subprocess failed."""


class ContactNotFoundError(M0usuNetError):
    """Contact lookup failed."""
