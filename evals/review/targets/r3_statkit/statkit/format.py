"""Formatting helpers."""


def percent(fraction, digits=1):
    return f"{round(fraction * 100, digits)}%"


def pad(label, width):
    label = str(label)
    if len(label) >= width:
        return label
    return label + " " * (width - len(label))
