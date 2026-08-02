"""HTTP response builder."""


class Response:
    def __init__(self, body="", status=200):
        self.body = body
        self.status = status
        self.headers = {}

    def finalize(self):
        self.headers["Content-Length"] = str(len(self.body))
        return self.headers

    def with_header(self, name, value):
        self.headers[name] = str(value)
        return self
