"""Claude response suggestions via Anthropic API."""

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

KEY_FILE = Path.home() / ".anthropic_api_key"

SYSTEM_PROMPT = """You are a texting assistant. Given a conversation between the user ("you") and a contact, suggest a short, natural reply the user could send. Match the tone and style of the user's previous messages. Keep it casual and brief — this is texting, not email. Return ONLY the suggested message text, nothing else."""


def _get_api_key() -> str:
    """Get API key from env var or ~/.anthropic_api_key file."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    if KEY_FILE.exists():
        return KEY_FILE.read_text().strip()
    return ""


def suggest_reply(messages: list[tuple[str, str, str]], contact_name: str) -> str:
    """Generate a reply suggestion using Claude.

    Args:
        messages: List of (direction, sender, body) tuples — recent conversation history.
        contact_name: The name of the person being texted.

    Returns:
        Suggested reply text, or error string prefixed with "ERROR: ".
    """
    try:
        import anthropic
    except ImportError:
        return "ERROR: anthropic package not installed"

    api_key = _get_api_key()
    if not api_key:
        return "ERROR: no API key (set ANTHROPIC_API_KEY or ~/.anthropic_api_key)"

    # Build conversation for Claude
    convo_lines = []
    for direction, sender, body in messages[-15:]:
        convo_lines.append(f"{sender}: {body}")
    convo_text = "\n".join(convo_lines)

    prompt = f"Here's a text conversation with {contact_name}:\n\n{convo_text}\n\nSuggest a reply for \"you\" to send next."

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        log.warning("Claude suggestion failed: %s", e)
        return f"ERROR: {e}"
