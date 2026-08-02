"""HTTP request wrapper."""


class Request:
    def __init__(self, method, path, headers=None, params={}):
        self.method = method
        self.path = path
        self.headers = headers or {}
        self.params = params

    def header(self, name):
        return self.headers.get(name)

    def is_json(self):
        ct = self.header("Content-Type") or ""
        return "application/json" in ct
