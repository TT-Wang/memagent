"""In-memory structured logging for taskdag."""

from collections import deque

#: Maximum number of entries the ring buffer holds before evicting the
#: oldest.
RING_SIZE = 200

#: In-memory ring buffer of ``(level, message)`` entries, newest last.
#: Once it holds :data:`RING_SIZE` entries, adding another drops the
#: oldest, so the buffer never grows beyond 200.
RING = deque(maxlen=RING_SIZE)


def log(level, msg):
    """Append the structured ``(level, msg)`` entry to :data:`RING`.

    ``level`` is a short lowercase tag such as ``"debug"``, ``"info"`` or
    ``"error"``; ``msg`` is the free-text message. The entry is added to
    the end of the ring, evicting the oldest entry when the ring is full.
    """
    RING.append((level, msg))
