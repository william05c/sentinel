"""Every module must import on a machine that only installed the declared dependencies.

lxml was installed by hand during development and never declared, so a clean checkout
could not import the text extractor. CI caught it; this catches it one step earlier.
"""

from __future__ import annotations

import importlib
import pkgutil

import sentinel


def test_every_module_imports():
    failures = []
    for module in pkgutil.walk_packages(sentinel.__path__, sentinel.__name__ + "."):
        try:
            importlib.import_module(module.name)
        except Exception as exc:  # report all of them, not just the first
            failures.append(module.name + ": " + type(exc).__name__ + " " + str(exc))
    assert not failures, "modules that fail to import: " + "; ".join(failures)
