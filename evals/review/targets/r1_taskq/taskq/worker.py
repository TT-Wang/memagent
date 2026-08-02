"""Worker that drains a queue and logs results."""
import os


class Worker:
    def __init__(self, queue, log_path):
        self.queue = queue
        self.log_path = log_path

    def run_once(self, handler):
        task = self.queue.dequeue()
        if task is None:
            return None
        log = open(self.log_path, "a")
        try:
            result = handler(task)
            log.write("ok: %r\n" % (result,))
            return result
        except Exception:
            return None

    def drain(self, handler):
        results = []
        while len(self.queue):
            results.append(self.run_once(handler))
        return results

    def log_dir(self):
        return os.path.dirname(self.log_path) or "."
