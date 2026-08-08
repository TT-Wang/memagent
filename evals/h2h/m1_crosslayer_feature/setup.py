import os


def _w(workdir, relpath, content):
    path = os.path.join(workdir, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def setup(workdir):
    pkg = "ledger"

    _w(workdir, os.path.join(pkg, "__init__.py"), '''\
"""ledger: a tiny layered accounts/transactions app.

Layered architecture (request flows top -> bottom, response bottom -> top):

    Controller  -> Service  -> Repository -> Store   (write/read path)
    Serializer  <- Service  <- ...                    (response envelope)

Public surface re-exported here for convenience.
"""
from .app import App
from .controller import AccountController
from .service import AccountService
from .repository import AccountRepository
from .store import Store
from .serializer import serialize_account, serialize_list
from .errors import NotFoundError, ForbiddenError

__all__ = [
    "App",
    "AccountController",
    "AccountService",
    "AccountRepository",
    "Store",
    "serialize_account",
    "serialize_list",
    "NotFoundError",
    "ForbiddenError",
]
''')

    _w(workdir, os.path.join(pkg, "errors.py"), '''\
"""Exception hierarchy for the ledger app."""


class LedgerError(Exception):
    pass


class NotFoundError(LedgerError):
    """Raised when no account matches the requested id (within scope)."""

    def __init__(self, account_id):
        self.account_id = account_id
        super().__init__("account %r not found" % (account_id,))


class ForbiddenError(LedgerError):
    """Raised when an account exists but belongs to a different tenant."""

    def __init__(self, account_id):
        self.account_id = account_id
        super().__init__("access to account %r is forbidden" % (account_id,))
''')

    _w(workdir, os.path.join(pkg, "models.py"), '''\
"""Value objects stored and passed between layers.

Each Account row ALREADY carries a `tenant_id` column in the store (the data
model is multi-tenant). The bug this app has today is that the *request path*
(controller -> service -> repository) ignores tenant_id entirely: any caller
can read or mutate any tenant's account, and the serialized response never
echoes which tenant the row belongs to.
"""
from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class Account:
    id: str
    tenant_id: str
    owner: str
    balance: int = 0
    meta: Dict[str, Any] = field(default_factory=dict)

    def copy(self):
        return Account(
            id=self.id,
            tenant_id=self.tenant_id,
            owner=self.owner,
            balance=self.balance,
            meta=dict(self.meta),
        )
''')

    _w(workdir, os.path.join(pkg, "store.py"), '''\
"""In-memory row store. The lowest layer; holds Account rows keyed by id.

The store is tenant-agnostic on purpose: it is a dumb key/value table that
stores whatever rows it is given (each row carries its own tenant_id field).
SCOPING by tenant is the *repository's* job, not the store's. Do NOT add tenant
filtering here -- the store must stay a plain table.
"""
from .models import Account


class Store:
    def __init__(self):
        self._rows = {}

    def insert(self, account):
        self._rows[account.id] = account.copy()
        return self._rows[account.id].copy()

    def get(self, account_id):
        row = self._rows.get(account_id)
        return row.copy() if row is not None else None

    def update(self, account):
        self._rows[account.id] = account.copy()
        return self._rows[account.id].copy()

    def all_rows(self):
        return [r.copy() for r in self._rows.values()]

    def seed(self, accounts):
        for a in accounts:
            self._rows[a.id] = a.copy()
''')

    # ---- LAYER 1: Repository (must SCOPE by tenant_id) ----
    _w(workdir, os.path.join(pkg, "repository.py"), '''\
"""LAYER 1: Repository. Mediates between the service and the raw Store.

REFACTOR TARGET: every read/write must be SCOPED to a tenant. Right now the
repository ignores tenant entirely -- it returns or mutates any row regardless
of which tenant owns it. Each method must accept a `tenant_id` and only ever
return / touch rows whose `row.tenant_id == tenant_id`; cross-tenant rows must
be invisible (treated as not found).
"""
from .models import Account


class AccountRepository:
    def __init__(self, store):
        self.store = store

    def get(self, account_id):
        """Return the Account, or None if missing. (Currently NOT tenant-scoped.)"""
        return self.store.get(account_id)

    def list(self):
        """Return all accounts. (Currently NOT tenant-scoped -- leaks tenants.)"""
        return self.store.all_rows()

    def create(self, account_id, owner, balance=0):
        """Create + persist a new account. (Currently stamps NO tenant_id.)"""
        acct = Account(id=account_id, tenant_id="", owner=owner, balance=balance)
        return self.store.insert(acct)

    def save(self, account):
        """Persist an updated account row."""
        return self.store.update(account)
''')

    # ---- LAYER 2: Service (must REQUIRE + FORWARD tenant_id, enforce ownership) ----
    _w(workdir, os.path.join(pkg, "service.py"), '''\
"""LAYER 2: Service. Business rules over the repository.

REFACTOR TARGET: the service is the layer that knows *which tenant* a request
belongs to. Every method must accept a `tenant_id`, forward it to the
repository for scoping, and enforce ownership: a get/deposit on an id that does
not exist within the tenant raises NotFoundError. Today none of that happens.
"""
from .errors import NotFoundError


class AccountService:
    def __init__(self, repository):
        self.repository = repository

    def open_account(self, account_id, owner, balance=0):
        """Open a new account for a tenant. (Currently drops tenant on the floor.)"""
        return self.repository.create(account_id, owner, balance)

    def get_account(self, account_id):
        """Fetch one account. (Currently returns any tenant's row.)"""
        acct = self.repository.get(account_id)
        if acct is None:
            raise NotFoundError(account_id)
        return acct

    def list_accounts(self):
        """List accounts. (Currently lists EVERY tenant's accounts.)"""
        return self.repository.list()

    def deposit(self, account_id, amount):
        """Add to an account's balance. (Currently mutates any tenant's row.)"""
        acct = self.repository.get(account_id)
        if acct is None:
            raise NotFoundError(account_id)
        acct.balance += amount
        return self.repository.save(acct)
''')

    # ---- LAYER 3: Controller (must READ tenant from request + thread it down) ----
    _w(workdir, os.path.join(pkg, "controller.py"), '''\
"""LAYER 3: Controller. Turns an inbound request dict into service calls.

A request is a plain dict, e.g.
    {"tenant_id": "acme", "action": "get", "account_id": "a1"}

REFACTOR TARGET: the controller must pull `tenant_id` out of the request and
thread it into every service call, then hand the result to the serializer so
the response is stamped with that tenant. Today it ignores request["tenant_id"]
completely and calls the service without it.
"""
from .serializer import serialize_account, serialize_list


class AccountController:
    def __init__(self, service):
        self.service = service

    def handle(self, request):
        action = request.get("action")
        if action == "open":
            acct = self.service.open_account(
                request["account_id"], request["owner"],
                request.get("balance", 0),
            )
            return serialize_account(acct)
        if action == "get":
            acct = self.service.get_account(request["account_id"])
            return serialize_account(acct)
        if action == "list":
            accts = self.service.list_accounts()
            return serialize_list(accts)
        if action == "deposit":
            acct = self.service.deposit(
                request["account_id"], request["amount"],
            )
            return serialize_account(acct)
        raise ValueError("unknown action %r" % (action,))
''')

    # ---- LAYER 4: Serializer (must STAMP tenant_id into the response) ----
    _w(workdir, os.path.join(pkg, "serializer.py"), '''\
"""LAYER 4: Serializer. Turns Account value objects into response dicts.

REFACTOR TARGET: the serialized response must include the account's
`tenant_id` under the key "tenant" so callers can see which tenant a row
belongs to. Today the serializer drops tenant_id entirely.
"""


def serialize_account(account):
    """Serialize one Account into a response dict. (Currently omits tenant.)"""
    return {
        "id": account.id,
        "owner": account.owner,
        "balance": account.balance,
    }


def serialize_list(accounts):
    """Serialize a list of Accounts into a list-response envelope."""
    return {
        "count": len(accounts),
        "items": [serialize_account(a) for a in accounts],
    }
''')

    _w(workdir, os.path.join(pkg, "app.py"), '''\
"""Top-level facade: wires Store -> Repository -> Service -> Controller.

This is the public entry point. App.request(req) drives the whole stack. The
seed wiring is correct; the bug is purely that tenant_id is not threaded
through the layers below.
"""
from .controller import AccountController
from .repository import AccountRepository
from .service import AccountService
from .store import Store


class App:
    def __init__(self, store=None):
        self.store = store or Store()
        self.repository = AccountRepository(self.store)
        self.service = AccountService(self.repository)
        self.controller = AccountController(self.service)

    def request(self, req):
        """Dispatch a single request dict through the controller."""
        return self.controller.handle(req)
''')

    # ---- DISTRACTOR: auth.py mentions a same-named LOCAL `tenant_id` that
    #      must NOT be rewired into the layered threading. ----
    _w(workdir, os.path.join(pkg, "auth.py"), '''\
"""Standalone token helpers. DISTRACTOR -- DO NOT change for this task.

This module has its OWN unrelated local variable also called `tenant_id`,
parsed out of an opaque API token string. It is NOT part of the
controller/service/repository/serializer request path and must stay exactly as
written. A blanket "thread tenant_id everywhere" edit that touches this file is
wrong: this tenant_id is a parsed token field, not the request-scoped tenant.
"""


def parse_token(token):
    """Split an 'tenant:user:nonce' API token into its parts.

    The local `tenant_id` here is purely a parsing detail of the token format
    and is intentionally independent of the request pipeline's tenant scoping.
    """
    parts = str(token).split(":")
    if len(parts) != 3:
        raise ValueError("malformed token %r" % (token,))
    tenant_id = parts[0]
    user = parts[1]
    nonce = parts[2]
    return {"tenant": tenant_id, "user": user, "nonce": nonce}


def is_valid(token):
    try:
        parse_token(token)
        return True
    except ValueError:
        return False
''')

    _w(workdir, os.path.join(pkg, "version.py"), '''\
"""Version metadata. DISTRACTOR -- DO NOT change for this task."""

__version__ = "2.1.0"


def version_tuple():
    return tuple(int(p) for p in __version__.split("."))
''')

    _w(workdir, "README.md", '''\
# ledger

A tiny layered accounts/transactions app.

```
Controller -> Service -> Repository -> Store
```

```python
from ledger import App
from ledger.models import Account

app = App()
app.store.seed([Account(id="a1", tenant_id="acme", owner="ann", balance=10)])
app.request({"tenant_id": "acme", "action": "get", "account_id": "a1"})
```

Each Account row carries a `tenant_id`. Requests are tenant-scoped: a request
for one tenant must never see, mutate, or leak another tenant's accounts, and
every response is stamped with the tenant it belongs to.
''')
