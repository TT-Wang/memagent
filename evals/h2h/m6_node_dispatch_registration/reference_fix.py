"""Reference solution (VALIDATION ONLY -- never shown to benchmarked agents).

Registers the new TryStar node consistently across the three dispatch tables:

  1. nodes.py     -- define class TryStar(Node) with _fields ("body","handlers")
                     and add TryStar to the ALL_NODE_CLASSES registry.
  2. rebuilder.py -- add Rebuilder.visit_TryStar (raw -> nodes.TryStar).
  3. as_string.py -- add AsStringVisitor.visit_TryStar (node -> 'try:' + 'except*').

raw.py and version.py are left byte-identical.
"""
import os


def _read(path):
    with open(path) as f:
        return f.read()


def _write(path, content):
    with open(path, "w") as f:
        f.write(content)


def apply(workdir):
    pkg = os.path.join(workdir, "miniast")

    # ----- Table #1: nodes.py -----
    nodes_path = os.path.join(pkg, "nodes.py")
    src = _read(nodes_path)

    # Insert TryStar class right after the Try class definition.
    try_class = '''\
class Try(Node):
    """A plain try/except statement: ``try: ... except E as n: ...``."""

    _fields = ("body", "handlers")
'''
    trystar_class = try_class + '''

class TryStar(Node):
    """A try/except* statement (exception groups): ``try: ... except* E: ...``."""

    _fields = ("body", "handlers")
'''
    assert try_class in src, "seed Try class not found in nodes.py"
    src = src.replace(try_class, trystar_class, 1)

    # Add TryStar to the ALL_NODE_CLASSES registry tuple.
    registry_old = '''\
ALL_NODE_CLASSES = (
    Module,
    Pass,
    Assign,
    Name,
    Const,
    ExceptHandler,
    Try,
)'''
    registry_new = '''\
ALL_NODE_CLASSES = (
    Module,
    Pass,
    Assign,
    Name,
    Const,
    ExceptHandler,
    Try,
    TryStar,
)'''
    assert registry_old in src, "seed ALL_NODE_CLASSES not found in nodes.py"
    src = src.replace(registry_old, registry_new, 1)
    _write(nodes_path, src)

    # ----- Table #2: rebuilder.py -----
    reb_path = os.path.join(pkg, "rebuilder.py")
    rsrc = _read(reb_path)
    try_visitor = '''\
    def visit_Try(self, raw):
        return nodes.Try(
            body=self._visit_all(raw["body"]),
            handlers=self._visit_all(raw["handlers"]),
        )
'''
    trystar_visitor = try_visitor + '''
    def visit_TryStar(self, raw):
        return nodes.TryStar(
            body=self._visit_all(raw["body"]),
            handlers=self._visit_all(raw["handlers"]),
        )
'''
    assert try_visitor in rsrc, "seed visit_Try not found in rebuilder.py"
    rsrc = rsrc.replace(try_visitor, trystar_visitor, 1)
    _write(reb_path, rsrc)

    # ----- Table #3: as_string.py -----
    asstr_path = os.path.join(pkg, "as_string.py")
    asrc = _read(asstr_path)
    try_render = '''\
    def visit_Try(self, node):
        out = "try:\\n" + self._block(node.body)
        for handler in node.handlers:
            out += "\\n" + self.visit(handler)
        return out
'''
    trystar_render = try_render + '''
    def _star_handler(self, node):
        """Render an ExceptHandler with the 'except*' (star) header form."""
        if node.type is None:
            head = "except*:"
        elif node.name:
            head = "except* %s as %s:" % (self.visit(node.type), node.name)
        else:
            head = "except* %s:" % (self.visit(node.type),)
        return head + "\\n" + self._block(node.body)

    def visit_TryStar(self, node):
        out = "try:\\n" + self._block(node.body)
        for handler in node.handlers:
            out += "\\n" + self._star_handler(handler)
        return out
'''
    assert try_render in asrc, "seed visit_Try not found in as_string.py"
    asrc = asrc.replace(try_render, trystar_render, 1)
    _write(asstr_path, asrc)
