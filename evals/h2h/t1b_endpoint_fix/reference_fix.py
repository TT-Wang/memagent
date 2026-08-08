import os

# Correct, full implementation of client/api.py after all seven turns.
# VALIDATION ONLY -- never shown to the benchmarked agents.
#
# The buried detail this reference honors: turn 3's parenthetical aside
# corrected the users endpoint from /api/users (stated in turn 1) to
# /api/v3/users. Both resource wrappers therefore hit /api/v3/users.

REFERENCE = '''\
"""High-level API client built on the Transport contract in transport.py."""
import json

from .transport import Transport


class ApiError(Exception):
    """Raised when the server answers with an error status."""

    def __init__(self, status, message=""):
        super().__init__(message or "API error %s" % status)
        self.status = status


class NotFoundError(ApiError):
    pass


class AuthError(ApiError):
    pass


class ServerError(ApiError):
    pass


def _error_for(status, message=""):
    if status == 404:
        return NotFoundError(status, message)
    if status in (401, 403):
        return AuthError(status, message)
    if status >= 500:
        return ServerError(status, message)
    return ApiError(status, message)


# Users endpoint: /api/v3/users (the unversioned /api/users is deprecated).
_USERS = "/api/v3/users"


class APIClient:
    def __init__(self, transport, api_token=None):
        self.transport = transport
        self.api_token = api_token

    # ----- auth ------------------------------------------------------------
    def set_token(self, token):
        self.api_token = token

    def clear_token(self):
        self.api_token = None

    def _headers(self, extra=None):
        h = {}
        if self.api_token:
            h["Authorization"] = "Bearer " + self.api_token
        if extra:
            h.update(extra)  # per-call headers win on collision
        return h

    # ----- response decoding -------------------------------------------------
    @staticmethod
    def _decode(status, headers, body):
        if status == 204 or not body:
            return None
        ctype = ""
        for k, v in (headers or {}).items():
            if k.lower() == "content-type":
                ctype = v or ""
        if "application/json" in ctype:
            return json.loads(body.decode("utf-8"))
        return body

    # ----- core request path ---------------------------------------------
    def _request(self, method, path, headers=None, params=None, body=None):
        status, rheaders, rbody = self.transport.request(
            method, path, headers=self._headers(headers), params=params,
            body=body)
        if status >= 400:
            message = ""
            try:
                parsed = self._decode(status, rheaders, rbody)
                if isinstance(parsed, dict):
                    message = parsed.get("message", "")
            except Exception:
                pass
            raise _error_for(status, message)
        return self._decode(status, rheaders, rbody)

    # ----- verbs ------------------------------------------------------------
    def get(self, path, params=None, headers=None):
        return self._request("GET", path, headers=headers, params=params)

    def post(self, path, json_body=None, params=None, headers=None):
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
        return self._request("POST", path, headers=h, params=params, body=body)

    def delete(self, path, params=None, headers=None):
        return self._request("DELETE", path, headers=headers, params=params)

    # ----- pagination -------------------------------------------------------
    def paginate(self, path, params=None):
        params = dict(params or {})
        while True:
            page = self.get(path, params=params)
            for item in (page or {}).get("items", []):
                yield item
            nxt = (page or {}).get("next")
            if not nxt:
                return
            params["cursor"] = nxt

    # ----- resource wrappers -------------------------------------------------
    def fetch_users(self):
        return self.get(_USERS)

    def delete_user(self, user_id):
        return self.delete("%s/%s" % (_USERS, user_id))
'''


def apply(workdir):
    api = os.path.join(workdir, "client", "api.py")
    with open(api, "w") as f:
        f.write(REFERENCE)
