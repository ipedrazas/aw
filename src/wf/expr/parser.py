"""Tokenizer and recursive-descent parser for the restricted grammar::

    expr    := or
    or      := and ('or' and)*
    and     := not ('and' not)*
    not     := 'not' not | cmp
    cmp     := sum (('==' | '!=' | '<' | '>') sum)?
    sum     := atom ('+' atom)*
    atom    := literal | path | '(' expr ')'
    path    := ident ('.' ident | '[*]' | '[' int ']')*
    literal := string | int | 'true' | 'false' | 'null'

Anything outside this grammar is a parse error, which the validator reports as a
``conflict`` finding: the definition cannot work as written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .ast import Add, And, Compare, Expr, Literal_, Not, Or, Path

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<int>-?\d+)
  | (?P<op>==|!=|<|>|\+|\(|\)|\.|\[\*\]|\[|\])
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<bad>.)
""",
    re.X,
)

KEYWORDS = {"and", "or", "not", "true", "false", "null"}


class ExprError(ValueError):
    pass


@dataclass(frozen=True)
class Tok:
    kind: str
    text: str
    pos: int


def tokenize(src: str) -> list[Tok]:
    toks: list[Tok] = []
    for m in _TOKEN.finditer(src):
        kind = m.lastgroup or "bad"
        if kind == "ws":
            continue
        if kind == "bad":
            raise ExprError(f"unexpected character {m.group()!r} at {m.start()} in {src!r}")
        toks.append(Tok(kind, m.group(), m.start()))
    return toks


class Parser:
    def __init__(self, src: str):
        self.src = src
        self.toks = tokenize(src)
        self.i = 0

    def peek(self) -> Tok | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self, kind: str | None = None, text: str | None = None) -> Tok:
        t = self.peek()
        if t is None:
            raise ExprError(f"unexpected end of expression in {self.src!r}")
        if kind and t.kind != kind:
            raise ExprError(f"expected {kind} but found {t.text!r} in {self.src!r}")
        if text and t.text != text:
            raise ExprError(f"expected {text!r} but found {t.text!r} in {self.src!r}")
        self.i += 1
        return t

    def at(self, text: str) -> bool:
        t = self.peek()
        return t is not None and t.text == text

    def parse(self) -> Expr:
        e = self.parse_or()
        if self.peek() is not None:
            raise ExprError(f"unexpected {self.peek().text!r} in {self.src!r}")  # type: ignore[union-attr]
        return e

    def parse_or(self) -> Expr:
        left = self.parse_and()
        while self.at("or"):
            self.take()
            left = Or(left, self.parse_and())
        return left

    def parse_and(self) -> Expr:
        left = self.parse_not()
        while self.at("and"):
            self.take()
            left = And(left, self.parse_not())
        return left

    def parse_not(self) -> Expr:
        if self.at("not"):
            self.take()
            return Not(self.parse_not())
        return self.parse_cmp()

    def parse_cmp(self) -> Expr:
        left = self.parse_sum()
        t = self.peek()
        if t and t.kind == "op" and t.text in ("==", "!=", "<", ">"):
            self.take()
            right = self.parse_sum()
            return Compare(t.text, left, right)  # type: ignore[arg-type]
        return left

    def parse_sum(self) -> Expr:
        left = self.parse_atom()
        while self.at("+"):
            self.take()
            left = Add(left, self.parse_atom())
        return left

    def parse_atom(self) -> Expr:
        t = self.peek()
        if t is None:
            raise ExprError(f"unexpected end of expression in {self.src!r}")
        if t.text == "(":
            self.take()
            e = self.parse_or()
            self.take(text=")")
            return e
        if t.kind == "string":
            self.take()
            return Literal_(_unquote(t.text))
        if t.kind == "int":
            self.take()
            return Literal_(int(t.text))
        if t.kind == "ident":
            if t.text in ("true", "false"):
                self.take()
                return Literal_(t.text == "true")
            if t.text == "null":
                self.take()
                return Literal_(None)
            if t.text in KEYWORDS:
                raise ExprError(f"unexpected keyword {t.text!r} in {self.src!r}")
            return self.parse_path()
        raise ExprError(f"unexpected {t.text!r} in {self.src!r}")

    def parse_path(self) -> Path:
        segs: list[str | int] = [self.take("ident").text]
        while True:
            if self.at("."):
                self.take()
                ident = self.take("ident")
                if ident.text in KEYWORDS:
                    raise ExprError(f"{ident.text!r} cannot be a field name in {self.src!r}")
                segs.append(ident.text)
            elif self.at("[*]"):
                self.take()
                segs.append("*")
            elif self.at("["):
                self.take()
                n = self.take("int")
                self.take(text="]")
                segs.append(int(n.text))
            else:
                break
        return Path(tuple(segs))


def _unquote(s: str) -> str:
    body = s[1:-1]
    return re.sub(r"\\(.)", r"\1", body)


def parse(src: str) -> Expr:
    """Parse one expression (the text inside ``${...}``)."""
    return Parser(src).parse()
