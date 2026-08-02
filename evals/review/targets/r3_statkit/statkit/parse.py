"""Parse rows of integers from text."""


def parse_ints(line):
    return [int(x) for x in line.split(",")]


def parse_table(text):
    rows = []
    for line in text.splitlines():
        if not line:
            continue
        rows.append(parse_ints(line))
    return rows
