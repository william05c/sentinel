"""Report rendering with bound numbers.

The rule this module enforces: no figure reaches a document except through a
binding, and a binding is a (query, parameters, formatter) triple that the verifier
can re-execute later. Prose is written by a human; numbers are written by SQL.

Mechanically it works like this:

    template  ->  {{ b("revenue_fy2024") }}  ->  \x00revenue_fy2024\x00
    renderer  ->  replaces the token with a formatted value, recording the binding
    verifier  ->  re-runs the recorded SQL and asserts the same string appears

If a number cannot be produced this way, it does not belong in the report.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

TOKEN = "\x00{binding}\x00"
TOKEN_RE = re.compile("\x00([^\x00]+)\x00")

FORMATTERS: dict[str, Callable[[float], str]] = {}


def formatter(name: str) -> Callable[[Callable[[float], str]], Callable[[float], str]]:
    def register(fn: Callable[[float], str]) -> Callable[[float], str]:
        FORMATTERS[name] = fn
        return fn
    return register


@formatter("money")
def _money(value: float) -> str:
    v = float(value)
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1e12:
        return sign + "$" + format(a / 1e12, ".2f") + "T"
    if a >= 1e9:
        return sign + "$" + format(a / 1e9, ".1f") + "B"
    if a >= 1e6:
        return sign + "$" + format(a / 1e6, ".1f") + "M"
    if a >= 1e3:
        return sign + "$" + format(a / 1e3, ".1f") + "K"
    return sign + "$" + format(a, ".0f")


@formatter("pct")
def _pct(value: float) -> str:
    return format(float(value) * 100.0, ".1f") + "%"


@formatter("ratio")
def _ratio(value: float) -> str:
    return format(float(value), ".2f") + "x"


@formatter("count")
def _count(value: float) -> str:
    return format(int(round(float(value))), ",d")


@formatter("raw")
def _raw(value: float) -> str:
    return format(float(value), ".4g")


def apply_format(fmt: str, value: float) -> str:
    if fmt not in FORMATTERS:
        raise KeyError("unknown formatter: " + fmt)
    return FORMATTERS[fmt](value)


@dataclass
class Binding:
    key: str
    sql: str
    params: list[Any]
    fmt: str
    rendered: str
    refs: list[str] = field(default_factory=list)
    uses: int = 0

    def to_json(self) -> dict:
        return {
            "key": self.key,
            "sql": self.sql,
            "params": list(self.params),
            "fmt": self.fmt,
            "rendered": self.rendered,
            "refs": list(self.refs),
            "uses": self.uses,
        }


class Binder:
    """Collects bindings while a template renders, then substitutes them in."""

    def __init__(self, con):
        self.con = con
        self._bindings: dict[str, Binding] = {}

    # -- used from templates -------------------------------------------------
    def b(self, key: str) -> str:
        if key not in self._bindings:
            raise KeyError("template asked for unbound key: " + key)
        return TOKEN.format(binding=key)

    # -- used from python ----------------------------------------------------
    def bind(
        self,
        key: str,
        sql: str,
        params: list[Any],
        *,
        fmt: str = "money",
        refs: list[str] | None = None,
    ) -> None:
        rows = self.con.execute(sql, params).fetchall()
        value = rows[0] if rows else None
        if value is None or value[0] is None:
            rendered = "n/a"
        else:
            rendered = apply_format(fmt, float(value[0]))
        self._bindings[key] = Binding(key, sql, list(params), fmt, rendered, refs or [])

    def values(self) -> dict[str, dict]:
        return {k: v.to_json() for k, v in self._bindings.items()}

    def finalize(self, text: str) -> str:
        unknown = set(TOKEN_RE.findall(text)) - set(self._bindings)
        if unknown:
            raise KeyError("unbound tokens in rendered output: " + ", ".join(sorted(unknown)))
        seen: dict[str, int] = {}
        for key in TOKEN_RE.findall(text):
            seen[key] = seen.get(key, 0) + 1
        for key, binding in self._bindings.items():
            binding.uses = seen.get(key, 0)
        return TOKEN_RE.sub(lambda m: self._bindings[m.group(1)].rendered, text)


def write_artifacts(
    out_dir: Path,
    *,
    stem: str,
    markdown: str,
    html: str,
    bindings: dict[str, dict],
    provenance: dict,
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "markdown": out_dir / (stem + ".md"),
        "html": out_dir / (stem + ".html"),
        "bindings": out_dir / (stem + ".bindings.json"),
        "provenance": out_dir / (stem + ".provenance.json"),
    }
    paths["markdown"].write_text(markdown, encoding="utf-8")
    paths["html"].write_text(html, encoding="utf-8")
    paths["bindings"].write_text(json.dumps(bindings, indent=1, default=str), encoding="utf-8")
    paths["provenance"].write_text(json.dumps(provenance, indent=1, default=str), encoding="utf-8")
    return paths
