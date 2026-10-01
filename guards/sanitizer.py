"""In-process check before the Judge. No model call (S-1, S-2)."""

import string

# No length number lives in THRESHOLDS.md. 2 000 covers every gold-set message.
MAX_CHARS = 2000
ALLOWED = set(string.ascii_letters + string.digits + string.whitespace)
# S-2: apostrophes and ordinary punctuation stay. Angle brackets stay too, so a
# script tag is the Judge's decision, not a character-list block.
ALLOWED.update("'`\"#$%:;@.,!?-_()/\\&*+=~^|[]{}<>")


def check(message):
    """Return (allowed, detail). A block here never calls the Judge (P-3)."""
    if len(message) > MAX_CHARS:
        return False, f"message is longer than {MAX_CHARS} characters"
    rejected = sorted({char for char in message if char not in ALLOWED})
    if rejected:
        return False, "character not allowed: " + " ".join(rejected)
    return True, "allowed"
