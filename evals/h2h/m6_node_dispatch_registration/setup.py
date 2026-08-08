import os


def _w(workdir, relpath, content):
    path = os.path.join(workdir, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def setup(workdir):
    """Write a small self-contained AST library, 'miniast'.

    miniast models the way a real AST/parsing toolkit (astroid) registers each
    node type across SEVERAL parallel dispatch tables that must stay in lockstep:

      * nodes.py      -- the node classes + the ALL_NODE_CLASSES registry
                         (mirrors astroid/nodes/node_classes.py + ALL_NODE_CLASSES)
      * rebuilder.py  -- maps a raw parse tree into miniast nodes via
                         visit_<RawType> dispatch (mirrors astroid/rebuilder.py)
      * as_string.py  -- the AsStringVisitor turns a node back into source via
                         visit_<nodename> dispatch (mirrors astroid/as_string.py)

    The base library supports Module / Pass / Assign / Name / Const / Try
    (with plain 'except'). It does NOT yet support the 'try/except*'
    (ExceptGroup) construct -- the TryStar node. The refactor adds it.

    Grounded in pylint-dev/astroid #2142 (astroid 2.15.4):
    "Add visitor function for TryStar to AsStringVisitor and add TryStar to
    astroid.nodes.ALL_NODE_CLASSES." Adding a node to astroid also requires the
    rebuilder mapping (ast.TryStar -> nodes.TryStar) -- exactly the three-table
    registration ported here.
    """
    pkg = "miniast"

    _w(workdir, os.path.join(pkg, "__init__.py"), '''\
"""miniast: a tiny AST round-trip toolkit.

Pipeline:  raw parse tree --(Rebuilder)--> miniast nodes --(AsStringVisitor)--> source

A "raw" parse tree is a plain nested structure (see raw.py) using the same
shape a real parser would emit. The Rebuilder converts each raw node into a
typed miniast node; AsStringVisitor turns a miniast node back into source text.

Every node type must be registered in THREE places that are kept in lockstep:
  * nodes.ALL_NODE_CLASSES   (the node-class registry)
  * Rebuilder.visit_<RawType> (raw -> node)
  * AsStringVisitor.visit_<nodename> (node -> source)
"""
from .nodes import (
    ALL_NODE_CLASSES,
    Assign,
    Const,
    ExceptHandler,
    Module,
    Name,
    Node,
    Pass,
    Try,
)
from .rebuilder import Rebuilder
from .as_string import AsStringVisitor, to_source
from .roundtrip import roundtrip

__all__ = [
    "ALL_NODE_CLASSES",
    "Node",
    "Module",
    "Pass",
    "Assign",
    "Name",
    "Const",
    "Try",
    "ExceptHandler",
    "Rebuilder",
    "AsStringVisitor",
    "to_source",
    "roundtrip",
]
''')

    # ----- nodes.py : node classes + the ALL_NODE_CLASSES registry -----
    _w(workdir, os.path.join(pkg, "nodes.py"), '''\
"""miniast node classes and the ALL_NODE_CLASSES registry.

REGISTRATION TABLE #1.  Every node type the library understands MUST appear in
ALL_NODE_CLASSES; tools iterate it to discover the supported grammar. A node
class also declares ``_fields`` so generic tree walks know its children.
"""


class Node:
    """Base node. Subclasses set a class-level ``_fields`` tuple."""

    _fields = ()

    def __init__(self, **kw):
        for name in self._fields:
            setattr(self, name, kw.get(name))

    def field_values(self):
        return [getattr(self, name) for name in self._fields]

    def __repr__(self):
        inner = ", ".join(
            "%s=%r" % (f, getattr(self, f)) for f in self._fields
        )
        return "%s(%s)" % (type(self).__name__, inner)


class Module(Node):
    _fields = ("body",)


class Pass(Node):
    _fields = ()


class Assign(Node):
    _fields = ("target", "value")


class Name(Node):
    _fields = ("id",)


class Const(Node):
    _fields = ("value",)


class ExceptHandler(Node):
    # type may be None (bare except); name is the bound exception variable.
    _fields = ("type", "name", "body")


class Try(Node):
    """A plain try/except statement: ``try: ... except E as n: ...``."""

    _fields = ("body", "handlers")


# REGISTRATION TABLE #1: the authoritative list of supported node classes.
# A node type that is missing here is considered unsupported by the library.
ALL_NODE_CLASSES = (
    Module,
    Pass,
    Assign,
    Name,
    Const,
    ExceptHandler,
    Try,
)


def is_supported(node_cls):
    """True if ``node_cls`` is a registered miniast node class."""
    return node_cls in ALL_NODE_CLASSES
''')

    # ----- rebuilder.py : raw parse tree -> miniast nodes -----
    _w(workdir, os.path.join(pkg, "rebuilder.py"), '''\
"""Rebuilder: turn a raw parse tree into typed miniast nodes.

REGISTRATION TABLE #2.  Dispatch is by the raw node's "kind": for a raw node
of kind ``X`` the Rebuilder calls ``self.visit_X(raw)``. Every supported node
type needs a ``visit_<RawKind>`` method here, or rebuilding raises
UnsupportedNodeError.

A raw node is a dict: {"kind": "<RawKind>", ...payload...}. See raw.py.
"""
from . import nodes


class UnsupportedNodeError(Exception):
    """Raised when the rebuilder meets a raw kind it has no visitor for."""

    def __init__(self, kind):
        self.kind = kind
        super().__init__("rebuilder has no visit_%s (unsupported node)" % (kind,))


class Rebuilder:
    """Recursively rebuild a raw tree into miniast Node objects."""

    def visit(self, raw):
        kind = raw["kind"]
        meth = getattr(self, "visit_" + kind, None)
        if meth is None:
            raise UnsupportedNodeError(kind)
        return meth(raw)

    def _visit_all(self, raws):
        return [self.visit(r) for r in raws]

    def visit_Module(self, raw):
        return nodes.Module(body=self._visit_all(raw["body"]))

    def visit_Pass(self, raw):
        return nodes.Pass()

    def visit_Assign(self, raw):
        return nodes.Assign(
            target=self.visit(raw["target"]),
            value=self.visit(raw["value"]),
        )

    def visit_Name(self, raw):
        return nodes.Name(id=raw["id"])

    def visit_Const(self, raw):
        return nodes.Const(value=raw["value"])

    def visit_ExceptHandler(self, raw):
        type_raw = raw.get("type")
        return nodes.ExceptHandler(
            type=self.visit(type_raw) if type_raw is not None else None,
            name=raw.get("name"),
            body=self._visit_all(raw["body"]),
        )

    def visit_Try(self, raw):
        return nodes.Try(
            body=self._visit_all(raw["body"]),
            handlers=self._visit_all(raw["handlers"]),
        )
''')

    # ----- as_string.py : miniast nodes -> source text -----
    _w(workdir, os.path.join(pkg, "as_string.py"), '''\
"""AsStringVisitor: render a miniast node back into source text.

REGISTRATION TABLE #3.  Dispatch is by the node's class name: for a node of
class ``X`` the visitor calls ``self.visit_X(node)``. Every supported node type
needs a ``visit_<ClassName>`` method here, or rendering raises
MissingVisitorError. This is the inverse of the rebuilder, so the two tables
must support exactly the same set of node types for a clean round-trip.
"""


class MissingVisitorError(Exception):
    """Raised when AsStringVisitor has no visit_<ClassName> for a node."""

    def __init__(self, name):
        self.name = name
        super().__init__("AsStringVisitor has no visit_%s" % (name,))


class AsStringVisitor:
    """Render miniast nodes back to source. Indentation is two spaces."""

    INDENT = "  "

    def visit(self, node):
        name = type(node).__name__
        meth = getattr(self, "visit_" + name, None)
        if meth is None:
            raise MissingVisitorError(name)
        return meth(node)

    def _block(self, body):
        """Render a suite of statements, indented one level."""
        lines = []
        for stmt in body:
            for line in self.visit(stmt).split("\\n"):
                lines.append(self.INDENT + line)
        return "\\n".join(lines)

    def visit_Module(self, node):
        return "\\n".join(self.visit(stmt) for stmt in node.body)

    def visit_Pass(self, node):
        return "pass"

    def visit_Assign(self, node):
        return "%s = %s" % (self.visit(node.target), self.visit(node.value))

    def visit_Name(self, node):
        return node.id

    def visit_Const(self, node):
        return repr(node.value)

    def visit_ExceptHandler(self, node):
        if node.type is None:
            head = "except:"
        elif node.name:
            head = "except %s as %s:" % (self.visit(node.type), node.name)
        else:
            head = "except %s:" % (self.visit(node.type),)
        return head + "\\n" + self._block(node.body)

    def visit_Try(self, node):
        out = "try:\\n" + self._block(node.body)
        for handler in node.handlers:
            out += "\\n" + self.visit(handler)
        return out


def to_source(node):
    """Convenience: render a node to source with a fresh visitor."""
    return AsStringVisitor().visit(node)
''')

    # ----- roundtrip.py : the public pipeline glue (NOT a distractor target,
    # but the agent generally need not edit it; it just chains the 3 tables) -----
    _w(workdir, os.path.join(pkg, "roundtrip.py"), '''\
"""Public pipeline glue: raw parse tree -> nodes -> source text."""
from .rebuilder import Rebuilder
from .as_string import to_source


def roundtrip(raw):
    """Rebuild ``raw`` into miniast nodes, then render back to source."""
    node = Rebuilder().visit(raw)
    return to_source(node)
''')

    # ----- raw.py : DISTRACTOR. The raw-grammar reference. It MENTIONS the
    # string "TryStar" only as documentation / a label in RAW_KINDS, but it is
    # NOT one of the three dispatch tables and must stay byte-identical. A
    # blanket "add TryStar everywhere" edit that also rewrites this file is wrong.
    _w(workdir, os.path.join(pkg, "raw.py"), '''\
"""Raw parse-tree helpers and the catalog of raw node kinds.

This module is the *input* side of the pipeline: it documents the raw node
shapes a parser may emit and offers tiny constructors for tests. It is NOT one
of the three registration tables (nodes.ALL_NODE_CLASSES, Rebuilder,
AsStringVisitor) and must not be edited to add node support -- the parser
already emits whatever kinds it wants; support is added downstream.

The catalog below intentionally lists EVERY kind a parser might emit, including
ones miniast does not yet rebuild (e.g. "TryStar"), so that this file stays a
stable reference regardless of which kinds are supported downstream.
"""

# Every raw kind a parser could emit. Listing a kind here does NOT mean miniast
# supports it -- support is determined by the three downstream tables.
RAW_KINDS = (
    "Module",
    "Pass",
    "Assign",
    "Name",
    "Const",
    "ExceptHandler",
    "Try",
    "TryStar",   # emitted by parsers for 'try/except*'; support added downstream
)


def name(ident):
    return {"kind": "Name", "id": ident}


def const(value):
    return {"kind": "Const", "value": value}


def assign(target, value):
    return {"kind": "Assign", "target": target, "value": value}


def module(*body):
    return {"kind": "Module", "body": list(body)}
''')

    # ----- version.py : DISTRACTOR #2. Pure metadata, must stay byte-identical.
    _w(workdir, os.path.join(pkg, "version.py"), '''\
"""Version metadata. DO NOT change for this task."""

__version__ = "0.3.0"


def version_tuple():
    return tuple(int(p) for p in __version__.split("."))
''')

    _w(workdir, "README.md", '''\
# miniast

A tiny AST round-trip toolkit.

```python
from miniast import roundtrip
raw = {"kind": "Module", "body": [{"kind": "Pass"}]}
print(roundtrip(raw))   # -> "  pass" inside a module
```

A node type is "supported" only when it is registered in all three tables:
`nodes.ALL_NODE_CLASSES`, `Rebuilder.visit_<RawKind>`, and
`AsStringVisitor.visit_<ClassName>`.
''')
