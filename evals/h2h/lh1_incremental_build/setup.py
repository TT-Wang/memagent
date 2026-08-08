import os


# setup() ships ONLY the end-goal contract (README.md) and an unimplemented
# stub (calc.py). NOTHING works until the agent builds it across the 8 turns:
# Calculator().eval(...) and the module-level evaluate(...) both raise
# NotImplementedError out of the box.

_README = '''\
calc -- an infix expression calculator
======================================

Goal (the END state we are building, incrementally, over several turns):
`calc.py` is a small but real arithmetic-expression evaluator. It is NOT a
toy that handles one example -- it must parse and evaluate arbitrary
well-formed expressions and report clear errors on malformed ones.

Public API (do NOT rename these -- the test harness imports them by name):

    class Calculator:
        def eval(self, line: str):
            """Evaluate one line. Returns the numeric result of an
            expression, or None if the line is an assignment `name = expr`.
            A single Calculator instance REMEMBERS variables across calls."""

    def evaluate(expr: str, env: dict | None = None):
        """Evaluate a single expression string and return its number.
        `env`, if given, supplies variable values for name lookups.
        Equivalent to a fresh Calculator seeded with `env`, evaluating one
        expression (never an assignment)."""

    class CalcError(Exception):
        """The single error type raised for every malformed input / runtime
        problem. No other (raw Python) exception should escape eval/evaluate."""

The grammar we are targeting (built up turn by turn):

  * integer and float literals:            42        3.5     0.25
  * the binary operators, by precedence (lowest first):
        +  -                 (left associative)
        *  /  //  %          (left associative)
        **                   (RIGHT associative, binds tighter than * /)
  * unary  + and -                          -5    --4    -(2+3)
  * parentheses for grouping                2 * (3 + 4)
  * variables (barewords) and assignment    x = 6      x * 7
  * built-in function calls                 abs sqrt pow min max
        abs(x)        exactly 1 arg
        sqrt(x)       exactly 1 arg
        pow(b, e)     exactly 2 args
        min(a, b, …)  2 or more args
        max(a, b, …)  2 or more args

Precedence / associativity rules that matter (Python-like):

    2 + 3 * 4      == 14         # * before +
    10 - 4 - 3     == 3          # - is left associative
    2 * 3 ** 2     == 18         # ** before *
    2 ** 3 ** 2    == 512        # ** is right associative
    -2 ** 2        == -4         # unary minus applies to the whole power
    2 ** -1        == 0.5        # a minus in the exponent is fine

Errors (every one of these must raise CalcError, not a raw Python error):

    division / floor-division / modulo by zero
    an unknown variable name            (message should name it)
    an unknown function                 (message should name it)
    a function called with wrong arity
    unbalanced parentheses
    a leftover / unexpected token        "1 2"   "1 +"   "* 3"
    empty or whitespace-only input
    sqrt of a negative number
'''


_STUB = '''\
"""calc -- infix expression calculator.

This is an UNIMPLEMENTED STUB. See README.md for the full target contract.
Build it out turn by turn: tokenizer + literals + - * /, then parentheses,
then // % **, then unary +/-, then right-associative **, then variables and
assignment, then built-in function calls, then uniform CalcError handling.
"""


class CalcError(Exception):
    """The single error type for every malformed input / runtime problem."""


class Calculator:
    """Stateful evaluator: remembers variables across .eval() calls."""

    def __init__(self, env=None):
        self.env = dict(env) if env else {}

    def eval(self, line):
        # TODO: tokenize `line`, parse it (respecting precedence and
        # associativity), evaluate it against self.env, and return the
        # numeric result -- or None for an assignment `name = expr`.
        raise NotImplementedError("calc.py is not implemented yet")


def evaluate(expr, env=None):
    # TODO: evaluate a single expression (never an assignment) using the
    # optional `env` dict for variable lookups, and return its number.
    raise NotImplementedError("calc.py is not implemented yet")
'''


def setup(workdir):
    """Write only the end-goal README and an unimplemented calc.py stub."""
    os.makedirs(workdir, exist_ok=True)

    with open(os.path.join(workdir, "README.md"), "w") as f:
        f.write(_README)

    with open(os.path.join(workdir, "calc.py"), "w") as f:
        f.write(_STUB)
