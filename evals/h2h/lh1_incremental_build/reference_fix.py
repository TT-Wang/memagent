import os


# The CORRECT, complete calc.py -- the END state of all 8 turns. Used ONLY to
# validate the scenario (no-op fails / reference passes); never shown to the
# agent. A clean recursive-descent evaluator implementing README.md's contract.

_SOLUTION = r'''"""calc -- infix expression calculator (recursive-descent).

Grammar (lowest precedence first):
    expr   := term (('+' | '-') term)*
    term   := unary (('*' | '/' | '//' | '%') unary)*
    unary  := ('+' | '-') unary | power
    power  := atom ('**' unary)?            # right associative
    atom   := NUMBER
            | NAME '(' args ')'             # function call
            | NAME                          # variable
            | '(' expr ')'
A line may also be an assignment:  NAME '=' expr  (eval returns None).
"""
import math


class CalcError(Exception):
    """The single error type for every malformed input / runtime problem."""


# --------------------------------------------------------------------------- tokenizer
_TWO_CHAR = {"**", "//"}
_ONE_CHAR = set("+-*/%()=,")


def _tokenize(s):
    toks = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
            continue
        # two-char operators first (** and //)
        pair = s[i:i + 2]
        if pair in _TWO_CHAR:
            toks.append(("op", pair))
            i += 2
            continue
        if c in _ONE_CHAR:
            kind = "assign" if c == "=" else ("comma" if c == "," else
                   ("lpar" if c == "(" else ("rpar" if c == ")" else "op")))
            toks.append((kind, c))
            i += 1
            continue
        if c.isdigit() or c == ".":
            j = i
            seen_dot = False
            while j < n and (s[j].isdigit() or s[j] == "."):
                if s[j] == ".":
                    if seen_dot:
                        raise CalcError("malformed number near %r" % s[i:j + 1])
                    seen_dot = True
                j += 1
            text = s[i:j]
            try:
                val = float(text) if seen_dot else int(text)
            except ValueError:
                raise CalcError("malformed number %r" % text)
            toks.append(("num", val))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (s[j].isalnum() or s[j] == "_"):
                j += 1
            toks.append(("name", s[i:j]))
            i = j
            continue
        raise CalcError("unexpected character %r" % c)
    return toks


# --------------------------------------------------------------------------- functions
def _fn_sqrt(args):
    if len(args) != 1:
        raise CalcError("sqrt() takes exactly 1 argument (%d given)" % len(args))
    x = args[0]
    if x < 0:
        raise CalcError("sqrt() of a negative number: %r" % x)
    return math.sqrt(x)


def _fn_abs(args):
    if len(args) != 1:
        raise CalcError("abs() takes exactly 1 argument (%d given)" % len(args))
    return abs(args[0])


def _fn_pow(args):
    if len(args) != 2:
        raise CalcError("pow() takes exactly 2 arguments (%d given)" % len(args))
    return args[0] ** args[1]


def _fn_min(args):
    if len(args) < 2:
        raise CalcError("min() takes 2 or more arguments (%d given)" % len(args))
    return min(args)


def _fn_max(args):
    if len(args) < 2:
        raise CalcError("max() takes 2 or more arguments (%d given)" % len(args))
    return max(args)


_FUNCS = {"sqrt": _fn_sqrt, "abs": _fn_abs, "pow": _fn_pow,
          "min": _fn_min, "max": _fn_max}


# --------------------------------------------------------------------------- parser
class _Parser:
    def __init__(self, toks, env):
        self.toks = toks
        self.pos = 0
        self.env = env

    def _peek(self):
        return self.toks[self.pos] if self.pos < len(self.toks) else (None, None)

    def _advance(self):
        t = self.toks[self.pos]
        self.pos += 1
        return t

    def parse_line(self):
        # assignment?  NAME '=' expr
        if (len(self.toks) >= 2 and self.toks[0][0] == "name"
                and self.toks[1][0] == "assign"):
            name = self.toks[0][1]
            self.pos = 2
            value = self.expr()
            self._expect_end()
            self.env[name] = value
            return None, True
        value = self.expr()
        self._expect_end()
        return value, False

    def _expect_end(self):
        if self.pos != len(self.toks):
            kind, val = self._peek()
            if kind == "rpar":
                raise CalcError("unmatched closing parenthesis ')'")
            raise CalcError("unexpected trailing token %r" % (val,))

    def expr(self):
        value = self.term()
        while True:
            kind, val = self._peek()
            if kind == "op" and val in ("+", "-"):
                self._advance()
                rhs = self.term()
                value = value + rhs if val == "+" else value - rhs
            else:
                return value

    def term(self):
        value = self.unary()
        while True:
            kind, val = self._peek()
            if kind == "op" and val in ("*", "/", "//", "%"):
                self._advance()
                rhs = self.unary()
                if val == "*":
                    value = value * rhs
                elif val == "/":
                    if rhs == 0:
                        raise CalcError("division by zero")
                    value = value / rhs
                elif val == "//":
                    if rhs == 0:
                        raise CalcError("division by zero")
                    value = value // rhs
                else:  # %
                    if rhs == 0:
                        raise CalcError("division by zero")
                    value = value % rhs
            else:
                return value

    def unary(self):
        kind, val = self._peek()
        if kind == "op" and val in ("+", "-"):
            self._advance()
            operand = self.unary()
            return operand if val == "+" else -operand
        return self.power()

    def power(self):
        base = self.atom()
        kind, val = self._peek()
        if kind == "op" and val == "**":
            self._advance()
            exp = self.unary()      # right associative; right operand may be unary
            return base ** exp
        return base

    def atom(self):
        kind, val = self._peek()
        if kind is None:
            raise CalcError("unexpected end of expression")
        if kind == "num":
            self._advance()
            return val
        if kind == "lpar":
            self._advance()
            value = self.expr()
            k2, _ = self._peek()
            if k2 != "rpar":
                raise CalcError("unmatched opening parenthesis '('")
            self._advance()
            return value
        if kind == "name":
            self._advance()
            k2, _ = self._peek()
            if k2 == "lpar":               # function call
                return self._call(val)
            if val in self.env:            # variable
                return self.env[val]
            raise CalcError("unknown variable %r" % val)
        if kind == "rpar":
            raise CalcError("unmatched closing parenthesis ')'")
        raise CalcError("unexpected token %r" % (val,))

    def _call(self, name):
        self._advance()  # consume '('
        args = []
        kind, _ = self._peek()
        if kind != "rpar":
            args.append(self.expr())
            while True:
                kind, _ = self._peek()
                if kind == "comma":
                    self._advance()
                    args.append(self.expr())
                else:
                    break
        kind, _ = self._peek()
        if kind != "rpar":
            raise CalcError("unmatched opening parenthesis '(' in call to %r" % name)
        self._advance()  # consume ')'
        fn = _FUNCS.get(name)
        if fn is None:
            raise CalcError("unknown function %r" % name)
        return fn(args)


# --------------------------------------------------------------------------- public API
def _run(line, env):
    if line is None or not str(line).strip():
        raise CalcError("empty input")
    toks = _tokenize(line)
    if not toks:
        raise CalcError("empty input")
    return _Parser(toks, env).parse_line()


class Calculator:
    """Stateful evaluator: remembers variables across .eval() calls."""

    def __init__(self, env=None):
        self.env = dict(env) if env else {}

    def eval(self, line):
        try:
            value, is_assign = _run(line, self.env)
        except CalcError:
            raise
        except Exception as e:  # never let a raw Python exception escape
            raise CalcError(str(e) or type(e).__name__)
        return None if is_assign else value


def evaluate(expr, env=None):
    calc = Calculator(env)
    value, is_assign = (None, False)
    try:
        value, is_assign = _run(expr, calc.env)
    except CalcError:
        raise
    except Exception as e:
        raise CalcError(str(e) or type(e).__name__)
    if is_assign:
        raise CalcError("evaluate() expects an expression, not an assignment")
    return value
'''


def apply(workdir):
    """Write the complete, correct calc.py (the END state of all 8 turns)."""
    with open(os.path.join(workdir, "calc.py"), "w") as f:
        f.write(_SOLUTION)
