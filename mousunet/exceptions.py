"""MousuNet exceptions."""


class MousuNetError(Exception):
    """Base exception."""


class RelayError(MousuNetError):
    """Relay subprocess failed."""


class ContactNotFoundError(MousuNetError):
    """Contact lookup failed."""
