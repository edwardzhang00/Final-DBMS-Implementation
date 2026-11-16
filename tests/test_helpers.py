"""Shared helpers and test data for the test suite."""
import contextlib
import io
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, '..'))
sys.path.insert(0, _HERE)

from dbms.ra.tokenizer import Tokenizer, LexError  # noqa: E402
from dbms.ra.parser import ParseError, parse_program, parse_query  # noqa: E402
from dbms.ra.tree import (  # noqa: E402
    render_tree,
    RelRef, Select, Project, Rename, Distinct, Union, Intersect, Minus, Times, Join,
    Comparison, And, Or, Not, AttrRef, Integer, String,
)
from dbms.ra.operators import (  # noqa: E402
    build_db, dict_lookup, eval_expr, reset_stats, STATS,
    EvalError, NameError_, SchemaError, TypeMismatchError, DuplicateAttrError,
    Relation, Column,
)


# Relations used by the semantic tests.  Deliberately includes:
#  - Employees / Dept for cases 19, 22, 23, 24, 25
#  - Emp with MgrID for the self-join (case 20)
#  - R with A=B for case 18
#  - A, B, C with single attribute x for the ambiguity demo (cases 10, 11)
RELATIONS = """
Employees (EID, Name, Age, DID) = {
  E1, John, 32, D1
  E2, Alice, 28, D2
  E3, Bob, 29, D1
}

Dept (DID, DName) = {
  D1, Sales
  D2, Eng
}

Emp (EID, MgrID, DID) = {
  E1, E1, D1
  E2, E1, D2
  E3, E1, D1
}

R (A, B) = {
  1, 1
  2, 2
  3, 1
}

S (B, C) = {
  1, 10
  2, 20
}

A (x) = {
  1
  2
}

B (x) = {
  2
}

C (x) = {
  1
}
"""


def tokenize_query(text):
    """Tokenize a query string, dropping NEWLINE and EOF tokens."""
    return [t for t in Tokenizer(text).tokenize()
            if t.kind not in ('NEWLINE', 'EOF')]


def build_test_db():
    """Build the shared test database from RELATIONS."""
    return build_db(parse_program(RELATIONS + "\nDummy\n"))


def run_command(main, argv):
    """Run a command line tool's main in this process: (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def eval_query(query, db):
    """Parse and evaluate a query, materialising the resulting bag as a list."""
    ast = parse_query(query)
    reset_stats()
    result = eval_expr(ast, dict_lookup(db))
    return Relation(result.name, result.columns, list(result.rows))