"""Database package."""

from .connection import get_connection, ensure_schema
from .models import Contact, Message, ConversationSummary

__all__ = ["get_connection", "ensure_schema", "Contact", "Message", "ConversationSummary"]
