"""SENTINEL — point-in-time public-company intelligence engine.

The package is organised around three layers, in dependency order:

    crawler   -> fetches bytes politely and keeps them immutably
    warehouse -> stores facts with provenance and answers as-of queries
    reports   -> renders numbers bound to queries, then proves they still hold

Nothing in this package may invent a number. Values come from the warehouse or
they do not exist.
"""

__version__ = "0.1.0"
PARSER_VERSION = "0.1.0"
SCHEMA_VERSION = 1
