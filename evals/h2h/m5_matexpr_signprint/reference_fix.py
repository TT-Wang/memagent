"""Reference solution (VALIDATION ONLY -- never shown to benchmarked agents).

Ports sympy PR #14248: fix MatAdd/MatMul sign printing consistently across the
str, latex, and pretty printers. The C code printer is left UNTOUCHED.

The shared contract applied to all three text printers:
  * _print_MatMul: if the leading factor is a negative number, drop it and
    emit a single leading '-' (MatMul(-1, A, B) -> '-A*B' etc.).
  * _print_MatAdd: build the sum term-by-term; if a printed term starts with
    '-', join it with subtraction instead of '+', and drop a leading '+'.
"""
import os


def _w(workdir, relpath, content):
    path = os.path.join(workdir, relpath)
    with open(path, "w") as f:
        f.write(content)


def apply(workdir):
    pkg = "matexpr"

    # --- str printer ---
    _w(workdir, os.path.join(pkg, "str_printer.py"), '''\
"""Plain-text printer (FIXED: pulls negative coefficient out as '-' and uses
subtraction in MatAdd)."""
from .printer import Printer


class StrPrinter(Printer):
    def _print_MatrixSymbol(self, expr):
        return expr.name

    def _print__Coeff(self, expr):
        return str(expr.value)

    def _print_MatMul(self, expr):
        coeff, rest = expr.as_coeff_mmul()
        if coeff < 0:
            sign = "-"
            factors = rest.args
        else:
            sign = ""
            factors = expr.args
        return sign + "*".join(self._print(arg) for arg in factors)

    def _print_MatAdd(self, expr):
        terms = [self._print(arg) for arg in expr.args]
        parts = []
        for t in terms:
            if t.startswith("-"):
                sign = "-"
                t = t[1:]
            else:
                sign = "+"
            parts.extend([sign, t])
        sign = parts.pop(0)
        if sign == "+":
            sign = ""
        return sign + " ".join(parts)


def sstr(expr):
    return StrPrinter().doprint(expr)
''')

    # --- latex printer ---
    _w(workdir, os.path.join(pkg, "latex_printer.py"), '''\
"""LaTeX printer (FIXED: same sign/subtraction contract as the str printer,
with single-space LaTeX product spacing)."""
from .printer import Printer


class LatexPrinter(Printer):
    def _print_MatrixSymbol(self, expr):
        return expr.name

    def _print__Coeff(self, expr):
        return str(expr.value)

    def _print_MatMul(self, expr):
        coeff, rest = expr.as_coeff_mmul()
        if coeff < 0:
            sign = "-"
            factors = rest.args
        else:
            sign = ""
            factors = expr.args
        return sign + " ".join(self._print(arg) for arg in factors)

    def _print_MatAdd(self, expr):
        terms = [self._print(arg) for arg in expr.args]
        parts = []
        for t in terms:
            if t.startswith("-"):
                sign = "-"
                t = t[1:]
            else:
                sign = "+"
            parts.extend([sign, t])
        sign = parts.pop(0)
        if sign == "+":
            sign = ""
        return sign + " ".join(parts)


def latex(expr):
    return LatexPrinter().doprint(expr)
''')

    # --- pretty printer ---
    _w(workdir, os.path.join(pkg, "pretty_printer.py"), '''\
"""'Pretty' printer (FIXED: same sign/subtraction contract, dot-separated
products)."""
from .printer import Printer

DOT = "\\u22c5"  # the MULTIPLICATION DOT used between matrix factors


class PrettyPrinter(Printer):
    def _print_MatrixSymbol(self, expr):
        return expr.name

    def _print__Coeff(self, expr):
        return str(expr.value)

    def _print_MatMul(self, expr):
        coeff, rest = expr.as_coeff_mmul()
        if coeff < 0:
            sign = "-"
            factors = rest.args
        else:
            sign = ""
            factors = expr.args
        return sign + DOT.join(self._print(arg) for arg in factors)

    def _print_MatAdd(self, expr):
        terms = [self._print(arg) for arg in expr.args]
        parts = []
        for t in terms:
            if t.startswith("-"):
                sign = "-"
                t = t[1:]
            else:
                sign = "+"
            parts.extend([sign, t])
        sign = parts.pop(0)
        if sign == "+":
            sign = ""
        return sign + " ".join(parts)


def pretty(expr):
    return PrettyPrinter().doprint(expr)
''')
