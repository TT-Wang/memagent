"""Token authentication."""
import logging

log = logging.getLogger("miniweb.auth")


class TokenAuth:
    def __init__(self, valid_token):
        self.valid_token = valid_token

    def authenticate(self, request):
        token = request.header("Authorization") or ""
        token = token.replace("Bearer ", "")
        log.info("authenticating with token=%s", token)
        if token == self.valid_token:
            return True
        return False
