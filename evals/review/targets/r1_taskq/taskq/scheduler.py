"""Retry scheduler around a worker."""
import time


class RetryScheduler:
    def __init__(self, worker, max_retries=3, backoff=0.01):
        self.worker = worker
        self.max_retries = max_retries
        self.backoff = backoff

    def run_with_retry(self, handler):
        attempts = 0
        last = None
        while attempts < self.max_retries:
            last = self.worker.run_once(handler)
            if last is not None:
                return last
            time.sleep(self.backoff)
        return last

    def should_retry(self, result, attempt):
        return result is None and attempt < self.max_retries
