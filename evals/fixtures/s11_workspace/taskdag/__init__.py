"""taskdag: a tiny task DAG toolkit over a plain dict registry."""

from .graph import (
    CycleError,
    add_task,
    by_tag,
    merge,
    remove_task,
    topo_order,
    waves,
)
from .registry import Registry
from .scheduler import RETRY_LIMIT, dry_run, run
from .stats import failures, results_to_json, summarize

__version__ = '1.0.0'

__all__ = [
    '__version__',
    'CycleError',
    'Registry',
    'RETRY_LIMIT',
    'add_task',
    'by_tag',
    'dry_run',
    'failures',
    'merge',
    'remove_task',
    'results_to_json',
    'run',
    'summarize',
    'topo_order',
    'waves',
]
