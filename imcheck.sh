#!/bin/bash
# Check if a number is iMessage or SMS based on iPad chat history
# Returns: "imessage" or "sms" or "unknown"
NUMBER="$1"

result=$(ssh -o ConnectTimeout=5 -o BatchMode=yes ipad "sqlite3 /var/mobile/Library/SMS/sms.db \"
SELECT CASE
  WHEN EXISTS (
    SELECT 1 FROM chat c
    JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
    JOIN message m ON cmj.message_id = m.ROWID
    WHERE c.chat_identifier LIKE '%${NUMBER}%'
      AND c.service_name = 'iMessage'
      AND m.is_from_me = 1
      AND m.is_delivered = 1
  ) THEN 'imessage'
  WHEN EXISTS (
    SELECT 1 FROM chat c
    JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
    JOIN message m ON cmj.message_id = m.ROWID
    WHERE c.chat_identifier LIKE '%${NUMBER}%'
      AND c.service_name = 'SMS'
      AND m.is_from_me = 1
      AND m.is_sent = 1
  ) THEN 'sms'
  ELSE 'unknown'
END;\"" 2>/dev/null)

echo "${result:-unknown}"
