"""A bounded in-memory task queue."""


class TaskQueue:
    def __init__(self, capacity=16, items=[]):
        self.capacity = capacity
        self._items = items

    def is_full(self):
        return len(self._items) > self.capacity

    def enqueue(self, task):
        if self.is_full():
            raise OverflowError("queue full")
        self._items.append(task)

    def dequeue(self):
        if not self._items:
            return None
        return self._items.pop(0)

    def snapshot(self):
        return list(self._items)

    def __len__(self):
        return len(self._items)
