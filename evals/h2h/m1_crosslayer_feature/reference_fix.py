"""Reference solution (VALIDATION ONLY -- never shown to benchmarked agents).

Threads the request's tenant_id through repository -> service -> controller and
stamps it in the serializer, USED at every layer. store.py / auth.py / version.py
are left untouched.
"""
import os


def _w(workdir, relpath, content):
    path = os.path.join(workdir, relpath)
    with open(path, "w") as f:
        f.write(content)


def apply(workdir):
    pkg = "ledger"

    # 1) Repository: scope every read/write by tenant_id.
    _w(workdir, os.path.join(pkg, "repository.py"), '''\
"""LAYER 1: Repository. Mediates between the service and the raw Store.

Every read/write is SCOPED to a tenant. Cross-tenant rows are invisible.
"""
from .models import Account


class AccountRepository:
    def __init__(self, store):
        self.store = store

    def get(self, tenant_id, account_id):
        """Return the Account only if it exists within the tenant, else None."""
        row = self.store.get(account_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    def list(self, tenant_id):
        """Return only rows belonging to the tenant."""
        return [r for r in self.store.all_rows() if r.tenant_id == tenant_id]

    def create(self, tenant_id, account_id, owner, balance=0):
        """Create + persist a new account stamped with the tenant."""
        acct = Account(
            id=account_id, tenant_id=tenant_id, owner=owner, balance=balance,
        )
        return self.store.insert(acct)

    def save(self, account):
        """Persist an updated account row."""
        return self.store.update(account)
''')

    # 2) Service: require + forward tenant_id, enforce ownership.
    _w(workdir, os.path.join(pkg, "service.py"), '''\
"""LAYER 2: Service. Business rules over the repository, tenant-scoped."""
from .errors import NotFoundError


class AccountService:
    def __init__(self, repository):
        self.repository = repository

    def open_account(self, tenant_id, account_id, owner, balance=0):
        return self.repository.create(tenant_id, account_id, owner, balance)

    def get_account(self, tenant_id, account_id):
        acct = self.repository.get(tenant_id, account_id)
        if acct is None:
            raise NotFoundError(account_id)
        return acct

    def list_accounts(self, tenant_id):
        return self.repository.list(tenant_id)

    def deposit(self, tenant_id, account_id, amount):
        acct = self.repository.get(tenant_id, account_id)
        if acct is None:
            raise NotFoundError(account_id)
        acct.balance += amount
        return self.repository.save(acct)
''')

    # 3) Controller: read request["tenant_id"] and thread it down.
    _w(workdir, os.path.join(pkg, "controller.py"), '''\
"""LAYER 3: Controller. Turns an inbound request dict into service calls."""
from .serializer import serialize_account, serialize_list


class AccountController:
    def __init__(self, service):
        self.service = service

    def handle(self, request):
        tenant_id = request["tenant_id"]
        action = request.get("action")
        if action == "open":
            acct = self.service.open_account(
                tenant_id, request["account_id"], request["owner"],
                request.get("balance", 0),
            )
            return serialize_account(acct)
        if action == "get":
            acct = self.service.get_account(tenant_id, request["account_id"])
            return serialize_account(acct)
        if action == "list":
            accts = self.service.list_accounts(tenant_id)
            return serialize_list(accts)
        if action == "deposit":
            acct = self.service.deposit(
                tenant_id, request["account_id"], request["amount"],
            )
            return serialize_account(acct)
        raise ValueError("unknown action %r" % (action,))
''')

    # 4) Serializer: stamp tenant_id under "tenant".
    _w(workdir, os.path.join(pkg, "serializer.py"), '''\
"""LAYER 4: Serializer. Turns Account value objects into response dicts."""


def serialize_account(account):
    """Serialize one Account into a response dict, stamped with its tenant."""
    return {
        "id": account.id,
        "owner": account.owner,
        "balance": account.balance,
        "tenant": account.tenant_id,
    }


def serialize_list(accounts):
    """Serialize a list of Accounts into a list-response envelope."""
    return {
        "count": len(accounts),
        "items": [serialize_account(a) for a in accounts],
    }
''')
