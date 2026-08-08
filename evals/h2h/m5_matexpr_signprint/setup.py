import os


def _w(workdir, relpath, content):
    path = os.path.join(workdir, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def setup(workdir):
    """Write a small self-contained 'matexpr' package that reproduces the
    sympy MatAdd/MatMul sign-printing bug (issue #14237 / PR #14248) at base
    state.

    Core idea (faithful to sympy): a difference A - B is represented internally
    as MatAdd(A, MatMul(-1, B)); the difference must PRINT as 'A - B', and a
    matmul with a leading negative coefficient (MatMul(-1, A, B)) must PRINT as
    '-A*B', NOT '(-1)*A*B'. The base-state printers naively join with ' + ' and
    do not pull the sign out of MatMul, so they print the (-1) coefficient and
    use '+' everywhere. The three concrete printers (str / latex / pretty) all
    share this same broken format contract and must be fixed CONSISTENTLY.
    """
    pkg = "matexpr"

    _w(workdir, os.path.join(pkg, "__init__.py"), '''\
"""matexpr: a tiny matrix-expression algebra + multi-printer system.

A miniature of sympy.matrices.expressions: symbolic MatrixSymbol, MatMul,
MatAdd, with several printers that must render the SAME expression
consistently (str / latex / pretty), plus a code printer for emitting C.
"""
from .core import MatrixSymbol, MatMul, MatAdd, MatExpr
from .printer import Printer
from .str_printer import StrPrinter, sstr
from .latex_printer import LatexPrinter, latex
from .pretty_printer import PrettyPrinter, pretty
from .code_printer import CodePrinter, ccode

__all__ = [
    "MatrixSymbol", "MatMul", "MatAdd", "MatExpr",
    "Printer",
    "StrPrinter", "sstr",
    "LatexPrinter", "latex",
    "PrettyPrinter", "pretty",
    "CodePrinter", "ccode",
]
''')

    _w(workdir, os.path.join(pkg, "core.py"), '''\
"""Symbolic matrix-expression algebra (a tiny slice of sympy).

The crucial modeling fact (matching sympy): subtraction is NOT a primitive.
``A - B`` is built as ``MatAdd(A, MatMul(-1, B))`` and ``-A`` as
``MatMul(-1, A)``. There is no negative MatrixSymbol; the sign lives on a
numeric coefficient inside a MatMul. The printers are responsible for turning
that internal (-1)-coefficient representation back into '-'/subtraction text.
"""


class MatExpr(object):
    """Base class for all matrix expressions. Provides operator sugar."""

    is_MatrixSymbol = False
    is_MatMul = False
    is_MatAdd = False

    # --- operator sugar that builds the internal representation ---
    def __mul__(self, other):
        other = _sympify(other)
        return MatMul(self, other)

    def __rmul__(self, other):
        other = _sympify(other)
        return MatMul(other, self)

    def __add__(self, other):
        return MatAdd(self, _sympify(other))

    def __sub__(self, other):
        # A - B  ==  A + (-1)*B   (exactly as sympy represents it)
        return MatAdd(self, MatMul(-1, _sympify(other)))

    def __neg__(self):
        return MatMul(-1, self)


def _sympify(x):
    """Coerce a Python int into a numeric coefficient MatExpr; pass MatExpr
    through unchanged."""
    if isinstance(x, MatExpr):
        return x
    if isinstance(x, int):
        return _Coeff(x)
    raise TypeError("cannot use %r in a matrix expression" % (x,))


class _Coeff(MatExpr):
    """A scalar numeric coefficient (only ever appears as the head of a
    MatMul, e.g. the (-1) in MatMul(-1, A)). Not part of the public surface."""

    is_Number = True

    def __init__(self, value):
        self.value = int(value)

    @property
    def args(self):
        return ()

    def __eq__(self, other):
        return isinstance(other, _Coeff) and other.value == self.value

    def __hash__(self):
        return hash(("_Coeff", self.value))


class MatrixSymbol(MatExpr):
    """A named symbolic matrix (the leaves of every expression)."""

    is_MatrixSymbol = True
    is_Number = False

    def __init__(self, name, rows=2, cols=2):
        self.name = name
        self.rows = rows
        self.cols = cols

    @property
    def args(self):
        return ()

    def __eq__(self, other):
        return (isinstance(other, MatrixSymbol)
                and other.name == self.name)

    def __hash__(self):
        return hash(("MatrixSymbol", self.name))


def _flatten(cls, operands):
    """Flatten nested instances of the same class (A*(B*C) -> A*B*C)."""
    flat = []
    for op in operands:
        if isinstance(op, cls):
            flat.extend(op.args)
        else:
            flat.append(op)
    return flat


class MatMul(MatExpr):
    """A product of factors. The first factor MAY be a numeric _Coeff
    (e.g. MatMul(-1, A, B) is the internal form of -A*B)."""

    is_MatMul = True
    is_Number = False

    def __init__(self, *operands):
        ops = [_sympify(o) for o in operands]
        self._args = tuple(_flatten(MatMul, ops))

    @property
    def args(self):
        return self._args

    def as_coeff_mmul(self):
        """Split off a leading numeric coefficient: returns (coeff, rest)
        where coeff is a Python int and rest is a MatMul of the remaining
        factors. If there is no leading numeric coefficient, coeff == 1.
        Mirrors sympy's MatMul.as_coeff_mmul()."""
        head = self._args[0]
        if getattr(head, "is_Number", False):
            return head.value, MatMul(*self._args[1:])
        return 1, self


class MatAdd(MatExpr):
    """A sum of terms. Subtraction is encoded as a term that is a MatMul with
    a leading negative coefficient (MatMul(-1, B))."""

    is_MatAdd = True
    is_Number = False

    def __init__(self, *operands):
        ops = [_sympify(o) for o in operands]
        self._args = tuple(_flatten(MatAdd, ops))

    @property
    def args(self):
        return self._args
''')

    _w(workdir, os.path.join(pkg, "printer.py"), '''\
"""Base Printer: dispatches on expression type to a _print_<Type> method.

Concrete printers (str / latex / pretty / code) subclass this and override the
_print_* methods. The MatAdd / MatMul methods here are the SHARED FORMAT
CONTRACT that the bug fix must update consistently across the text printers.
"""


class Printer(object):
    """Visitor base: doprint() -> _print() -> _print_<ClassName>()."""

    def doprint(self, expr):
        return self._print(expr)

    def _print(self, expr):
        for cls in type(expr).__mro__:
            meth = "_print_" + cls.__name__
            if hasattr(self, meth):
                return getattr(self, meth)(expr)
        return self._print_default(expr)

    def _print_default(self, expr):
        raise NotImplementedError(
            "%s has no _print_%s" % (type(self).__name__,
                                     type(expr).__name__))
''')

    # ---- str printer (BUGGY base state) ----
    _w(workdir, os.path.join(pkg, "str_printer.py"), '''\
"""Plain-text printer. BUGGY base state: prints the internal (-1) coefficient
and joins MatAdd terms with ' + ', so A - A*B - B renders as
'(-1)*B + (-1)*A*B + A' instead of the desired '-B - A*B + A'.
"""
from .printer import Printer


class StrPrinter(Printer):
    def _print_MatrixSymbol(self, expr):
        return expr.name

    def _print__Coeff(self, expr):
        return str(expr.value)

    def _print_MatMul(self, expr):
        # BUG: does not pull a leading negative coefficient out as a '-' sign,
        # so MatMul(-1, A, B) prints as '(-1)*A*B'.
        parts = []
        for arg in expr.args:
            if getattr(arg, "is_Number", False) and arg.value < 0:
                parts.append("(%d)" % arg.value)
            else:
                parts.append(self._print(arg))
        return "*".join(parts)

    def _print_MatAdd(self, expr):
        # BUG: always joins with ' + ', never turns a negative term into
        # subtraction.
        return " + ".join(self._print(arg) for arg in expr.args)


def sstr(expr):
    return StrPrinter().doprint(expr)
''')

    # ---- latex printer (BUGGY base state) ----
    _w(workdir, os.path.join(pkg, "latex_printer.py"), '''\
"""LaTeX printer. BUGGY base state mirrors the str printer's flaw: it prints
the (-1) coefficient and joins with ' + '. Spacing uses a single space, the
LaTeX convention here (e.g. 'A B' for a product, 'A + B' for a sum).
"""
from .printer import Printer


class LatexPrinter(Printer):
    def _print_MatrixSymbol(self, expr):
        return expr.name

    def _print__Coeff(self, expr):
        return str(expr.value)

    def _print_MatMul(self, expr):
        # BUG: prints leading negative coefficient instead of a '-' sign.
        parts = []
        for arg in expr.args:
            if getattr(arg, "is_Number", False) and arg.value < 0:
                parts.append("(%d)" % arg.value)
            else:
                parts.append(self._print(arg))
        return " ".join(parts)

    def _print_MatAdd(self, expr):
        # BUG: always joins terms with ' + '.
        return " + ".join(self._print(arg) for arg in expr.args)


def latex(expr):
    return LatexPrinter().doprint(expr)
''')

    # ---- pretty printer (BUGGY base state) ----
    _w(workdir, os.path.join(pkg, "pretty_printer.py"), '''\
"""'Pretty' (unicode-ish) printer. Same shared bug: it joins MatAdd terms with
a ' + ' and renders MatMul with a center-dot, but prints the (-1) coefficient
verbatim. The desired output uses subtraction and a leading '-'.
"""
from .printer import Printer

DOT = "\\u22c5"  # the MULTIPLICATION DOT used between matrix factors


class PrettyPrinter(Printer):
    def _print_MatrixSymbol(self, expr):
        return expr.name

    def _print__Coeff(self, expr):
        return str(expr.value)

    def _print_MatMul(self, expr):
        # BUG: prints leading negative coefficient instead of a '-' sign.
        parts = []
        for arg in expr.args:
            if getattr(arg, "is_Number", False) and arg.value < 0:
                parts.append("(%d)" % arg.value)
            else:
                parts.append(self._print(arg))
        return DOT.join(parts)

    def _print_MatAdd(self, expr):
        # BUG: always joins terms with ' + '.
        return " + ".join(self._print(arg) for arg in expr.args)


def pretty(expr):
    return PrettyPrinter().doprint(expr)
''')

    # ---- code printer (DISTRACTOR: must NOT change) ----
    _w(workdir, os.path.join(pkg, "code_printer.py"), '''\
"""C-code printer (DISTRACTOR -- DO NOT change for this fix).

This printer deliberately renders MatMul/MatAdd through helper FUNCTION calls
(matmul(...) / matadd(...)) rather than infix operators, because C has no
matrix operators. Its MatAdd/MatMul methods look superficially like the text
printers (same method names, same '_Coeff' handling), so a careless
find/replace or 'fix every _print_MatAdd' pass would wrongly edit it -- but the
sign/subtraction contract is a TEXT-rendering concern and does NOT apply to
the generated C function-call form. The (-1) coefficient is a legitimate
argument to the C helper and must stay. Changing this file is WRONG.
"""
from .printer import Printer


class CodePrinter(Printer):
    def _print_MatrixSymbol(self, expr):
        return expr.name

    def _print__Coeff(self, expr):
        return str(expr.value)

    def _print_MatMul(self, expr):
        # Emit a nested C helper call. The numeric coefficient (incl. -1) is a
        # real argument to matmul() and is printed as-is on purpose.
        args = ", ".join(self._print(arg) for arg in expr.args)
        return "matmul(%s)" % args

    def _print_MatAdd(self, expr):
        args = ", ".join(self._print(arg) for arg in expr.args)
        return "matadd(%s)" % args


def ccode(expr):
    return CodePrinter().doprint(expr)
''')

    _w(workdir, os.path.join(pkg, "version.py"), '''\
"""Version metadata. DO NOT change for this fix."""

__version__ = "0.3.0"


def version_tuple():
    return tuple(int(p) for p in __version__.split("."))
''')

    _w(workdir, "README.md", '''\
# matexpr

A tiny symbolic matrix-expression algebra with several printers.

```python
from matexpr import MatrixSymbol, sstr, latex, pretty

A = MatrixSymbol("A")
B = MatrixSymbol("B")
print(sstr(A - A * B - B))   # should be:  -B - A*B + A
```

Internally a difference `A - B` is `MatAdd(A, MatMul(-1, B))`. Each printer
(`str`, `latex`, `pretty`) must turn that internal `(-1)` coefficient back
into a `-` sign and use subtraction, consistently. The C `code` printer is
different: it renders through helper function calls and is not part of the
sign/subtraction contract.
''')
