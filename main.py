"""Backward-compatible entry point.

The app now lives in :mod:`arb.app`. This file keeps the original
``python main.py`` invocation working (as documented in the README) and
simply delegates to it.

Preferred entry points (any of these are equivalent):

    python -m arb        # from the project root
    arbbot               # after `pip install -e .`
    python main.py       # legacy / convenience shim (this file)
"""
from arb.app import main

if __name__ == "__main__":
    main()
