"""Obvious attacks the Judge may block without a model call."""

import re

# Specific on purpose. "drop", "select", "delete" and apostrophes are not enough.
_PATTERNS = (
    (r"(?i)\bdrop\s+table\b", "SQL DROP TABLE"),
    (r"(?i)\bunion\s+select\b", "SQL UNION SELECT"),
    (r"(?i)\bdelete\s+from\b", "SQL DELETE FROM"),
    (r"(?i)\bupdate\s+\w+\s+set\b", "SQL UPDATE SET"),
    (r"(?i)\bselect\s+.+\s+from\s+\w+", "SQL SELECT FROM"),
    (r"(?i)'\s*or\b", "SQL tautology"),
    (r"(?i)\bor\s+1\s*=\s*1\b", "SQL OR 1=1"),
    (r"(?i)admin'\s*--", "SQL comment"),
    (r"(?i)<\s*script\b", "script tag"),
    (r"(?i)<\s*iframe\b", "iframe tag"),
    (r"(?i)\bonerror\s*=", "inline event handler"),
    (r"(?i)javascript\s*:", "javascript URL"),
    (r"\{\{.*?\}\}", "template injection"),
    (r"(?i)\$\([^)]*\)", "shell substitution"),
    (r"(?i);\s*cat\s+/", "shell command"),
    (r"\.\./", "path traversal"),
    (r"(?i)ignore all previous instructions", "prompt extraction"),
    (r"(?i)ignore the above", "prompt extraction"),
    (r"(?i)you are now dan", "jailbreak"),
    (r"(?i)^system\s*:", "forged system turn"),
    (r"(?i)forget your rules", "prompt extraction"),
    (r"(?i)pretend the security judge", "forged approval"),
    (r"(?i)new instructions\s*:", "prompt injection"),
    (r"(?i)print your (system )?prompt", "prompt extraction"),
    (r"(?i)print your instructions", "prompt extraction"),
    (r"(?i)repeat everything above", "prompt extraction"),
    (r"(?i)system prompt", "prompt extraction"),
)


def obvious_attack(message):
    """Return a reason when the message is an obvious attack, else None."""
    for pattern, reason in _PATTERNS:
        if re.search(pattern, message):
            return reason
    return None
