"""Central, mutable configuration for taskdag."""

import json

#: Log levels accepted by :func:`validate` (lowercase, mirroring Python's
#: ``logging`` levels).
VALID_LOG_LEVELS = ("debug", "info", "warning", "error", "critical")

CONFIG = {
    "worker_count": 4,
    "wave_pause_ms": 50,
    "log_level": "info",
    "qz_max_queue_depth": 128,
    "qz_batch_flush_size": 32,
    "qz_color_output": True,
    "qz_trace_enabled": False,
    "qz_retry_limit": 3,
    "qz_gateway_retry_limit": 3,
    "qz_export_pretty": False,
    "qz_demo_retry_limit": 3,
}

#: The keys :func:`validate` recognizes -- the seeded schema. Any other
#: key in a config mapping is unknown: ``qz_``-prefixed unknown keys are
#: tolerated with a warning (see :data:`WARNINGS`), all others raise.
KNOWN_KEYS = frozenset(CONFIG)

#: Warnings collected by :func:`validate` / :func:`load` for unknown
#: ``qz_``-prefixed keys, newest last. Cleared at the start of each
#: validation, so the list always reflects the most recent call.
WARNINGS = []


def get(key, default=None):
    """Return the config value for ``key``, or ``default`` if unset."""
    return CONFIG.get(key, default)


def get_int(key):
    """Return the config value for ``key`` coerced to ``int``.

    Unlike :func:`get` there is no default: a missing key raises
    :class:`KeyError`. A stored value that is not int-like raises
    :class:`ValueError` (bools are rejected, mirroring :func:`validate`).
    """
    if key not in CONFIG:
        raise KeyError(key)
    value = CONFIG[key]
    if isinstance(value, bool):
        raise ValueError(f"config key {key!r} holds a bool, not an int")
    return int(value)


def set_key(key, value):
    """Set the config value for ``key`` to ``value``."""
    CONFIG[key] = value


def _validate_config(config):
    """Validate a config mapping (shared by :func:`validate` and :func:`load`).

    Unknown keys raise :class:`ValueError`, except ``qz_``-prefixed ones,
    which append a warning to :data:`WARNINGS` instead. Raises
    :class:`ValueError` if any key ending in ``_count`` or ``_ms`` does
    not hold an ``int``, or if ``log_level`` is not one of the known
    levels in :data:`VALID_LOG_LEVELS`.
    """
    for key, value in config.items():
        if key not in KNOWN_KEYS:
            if key.startswith("qz_"):
                WARNINGS.append(f"unknown qz_ config key {key!r}")
            else:
                raise ValueError(f"unknown config key {key!r}")
            continue
        if key.endswith("_count") or key.endswith("_ms"):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(
                    f"config key {key!r} must be an int, got {type(value).__name__!r}"
                )
    if "log_level" in config and config["log_level"] not in VALID_LOG_LEVELS:
        raise ValueError(
            f"unknown log level {config['log_level']!r}; expected one of "
            + ", ".join(VALID_LOG_LEVELS)
        )


def validate():
    """Validate the current :data:`CONFIG`.

    Raises :class:`ValueError` for unknown non-``qz_`` keys, for any key
    ending in ``_count`` or ``_ms`` that does not hold an ``int``, or for
    an unknown ``log_level``. Unknown ``qz_``-prefixed keys are
    tolerated: a warning is appended to :data:`WARNINGS` (cleared first,
    so the list reflects this call) instead of raising.
    """
    WARNINGS.clear()
    _validate_config(CONFIG)


def dump(path):
    """Write the current :data:`CONFIG` to ``path`` as JSON."""
    with open(path, "w") as f:
        json.dump(CONFIG, f, indent=2, sort_keys=True)


def load(path):
    """Replace :data:`CONFIG` with the JSON document read from ``path``.

    The loaded values are validated first; on invalid values a
    :class:`ValueError` is raised and :data:`CONFIG` is left unchanged.
    Unknown ``qz_``-prefixed keys are tolerated: a warning is appended to
    :data:`WARNINGS` (cleared first) and the key is loaded anyway.
    """
    with open(path) as f:
        data = json.load(f)
    WARNINGS.clear()
    _validate_config(data)
    CONFIG.clear()
    CONFIG.update(data)
