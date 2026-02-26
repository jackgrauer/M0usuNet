"""Pydantic models."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class Contact(BaseModel):
    id: int
    display_name: str
    phone: Optional[str] = None
    created_at: Optional[datetime] = None


class Message(BaseModel):
    id: int
    contact_id: int
    platform: str
    direction: str  # "in" or "out"
    body: str
    sent_at: Optional[datetime] = None
    delivered: bool = False
    relay_output: Optional[str] = None
    external_guid: Optional[str] = None


class ConversationSummary(BaseModel):
    contact_id: int
    display_name: str
    phone: Optional[str] = None
    platform: str
    last_message: str
    last_time: Optional[datetime] = None
    direction: str
