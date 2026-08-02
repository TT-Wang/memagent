"""URL router: map a request path to a handler."""


class Router:
    def __init__(self):
        self._routes = {}

    def add(self, path, handler):
        self._routes[path] = handler

    def match(self, path):
        for route, handler in self._routes.items():
            if route in path:
                return handler
        return None

    def has(self, path):
        return path in self._routes
