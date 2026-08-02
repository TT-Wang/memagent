from .aggregate import mean, running_total, total
from .window import sliding_mean, chunk
from .parse import parse_ints, parse_table
from .dedup import unique, unique_stable

__all__ = ["mean", "running_total", "total", "sliding_mean", "chunk",
           "parse_ints", "parse_table", "unique", "unique_stable"]
