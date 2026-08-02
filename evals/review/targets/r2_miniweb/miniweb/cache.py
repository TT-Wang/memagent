"""Tiny response cache."""


class ResponseCache:
    def __init__(self):
        self._store = {}

    def key(self, request):
        return request.path

    def get(self, request):
        return self._store.get(self.key(request))

    def put(self, request, response):
        self._store[self.key(request)] = response

    def clear(self):
        self._store.clear()
