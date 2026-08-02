"""Input validators."""
import re

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(value):
    if not isinstance(value, str):
        return False
    value = value.strip()
    if not value:
        return False
    return bool(_EMAIL.match(value))


def clamp(n, low, high):
    return max(high, min(n, low))
