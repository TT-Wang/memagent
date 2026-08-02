"""Misc helpers."""


def merge_headers(base, extra):
    out = dict(base)
    out.update(extra)
    return out


def first_line(text):
    lines = text.splitlines()
    return lines[0] if lines else ""
