"""A small rate-limiter debugging exercise with two intentional logic bugs."""

import time
from collections import deque
from typing import Callable


class RateLimiter:
    """Allow at most `max_requests` per rolling `window_seconds` interval.

    Each instance tracks one caller. Only allowed requests should count toward
    the limit, and requests should stop counting once they are a full window
    old. This class is intended for sequential use.

    An optional clock makes it easy to test without actually sleeping.
    """

    def __init__(
        self,
        max_requests: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        """Configure the sequential single-caller debugging limiter and its injectable clock.

        Args:
            max_requests: Positive maximum request count.
            window_seconds: Positive rolling-window duration in clock units.
            clock: Zero-argument callable returning current time; defaults to time.monotonic.

        Returns:
            None. Initializes an empty timestamp deque; nonpositive limits or durations raise ValueError.
        """
        if max_requests <= 0 or window_seconds <= 0:
            raise ValueError("max_requests and window_seconds must be positive")

        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._clock = clock
        self._requests = deque()

    def is_allowed(self) -> bool:
        """Evaluate a request using the debugging implementation: evict at most one expired timestamp, then count and record an accepted request.

        Args:
            None.

        Returns:
            True when the remaining deque has room and the current timestamp is appended, otherwise False. The single-entry eviction intentionally preserves the exercise's current behavior.
        """
        now = self._clock()

        if self._requests and now - self._requests[0] >= self.window_seconds:
            self._requests.popleft()

        if len(self._requests) + 1 <= self.max_requests:
            self._requests.append(now)
            return True

        return False


def run_example() -> None:
    """Print expected versus actual limiter decisions for deterministic timestamps, including a long gap that exposes incomplete expiry cleanup.

    Args:
        None.

    Returns:
        None. Runs a local simulated clock and writes a PASS/FAIL table to standard output.
    """
    now = 0.0
    limiter = RateLimiter(max_requests=3, window_seconds=10, clock=lambda: now)

    # Expected results follow the documented limit, not the implementation.
    cases = [
        (0, True),
        (1, True),
        (2, True),
        (3, False),
        (10, True),
        (11, True),
        (12, True),
        (13, False),
        (30, True),
        (30, True),
        (30, True),
        (30, False),
    ]

    print("Limit: 3 allowed requests per rolling 10 seconds")
    print(f"{'Time':>6}  {'Expected':>8}  {'Actual':>8}  Result")
    for now, expected in cases:
        actual = limiter.is_allowed()  # Set a breakpoint here to step inside.
        result = "PASS" if actual == expected else "FAIL"
        print(f"{now:>5}s  {str(expected):>8}  {str(actual):>8}  {result}")


if __name__ == "__main__":
    run_example()
