from collections import defaultdict, deque
class RateLimiter:
    def __init__(self, window, limit):
        """Initialize per-user timestamp buckets for a rolling-window request limit.

        Args:
            window: Number of integer timestamp units included in each rolling window.
            limit: Maximum counted requests permitted per user in that window.

        Returns:
            None. Initializes a dictionary of per-user deques without validating configuration.
        """
        self.window = window
        self.limit = limit
        self.users = defaultdict(deque)

    def is_allowed(self, user_id, timestamp):
        """Expire old buckets for one user, reject requests at the limit, and record accepted requests in their integer-time bucket.

        Args:
            user_id: Hashable user identity whose request history is tracked independently.
            timestamp: Request time converted to an integer; callers should provide nondecreasing times per user.

        Returns:
            True when accepted and counted, False at the limit. Mutates that user's history and removes buckets strictly before the inclusive cutoff.
        """
        timestamp = int(timestamp)
        cutoff = timestamp - self.window + 1
        while self.users[user_id] and self.users[user_id][0][1] < cutoff:
            self.users[user_id].popleft()

        total = 0
        for i in self.users[user_id]:
            total += i[0]
        if total >= self.limit:
            return False

        if self.users[user_id] and self.users[user_id][-1][1] == timestamp:
            self.users[user_id][-1][0] += 1
        else:
            self.users[user_id].append([1, timestamp])
        return True
