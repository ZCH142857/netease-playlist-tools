from __future__ import annotations

import time
from collections.abc import Callable
from math import ceil

HEART_INTERVAL_SECONDS = 4.0
RETRY_COOLDOWN_SECONDS = 60.0


def wait_until(
    deadline: float,
    should_stop: Callable[[], bool],
    countdown: Callable[[int], None] | None = None,
) -> bool:
    previous = 0
    while (remaining := deadline - time.monotonic()) > 0:
        if should_stop():
            return False
        seconds = ceil(remaining)
        if countdown is not None and seconds != previous:
            countdown(seconds)
            previous = seconds
        time.sleep(min(0.1, remaining))
    return not should_stop()
