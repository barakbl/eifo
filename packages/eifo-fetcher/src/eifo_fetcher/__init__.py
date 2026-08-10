"""Eifo catalog fetcher.

Owns every write to the catalog tables. Source plugins, the title matcher and
the enrichers arrive in stages S1 and S2; stage S0 ships the CLI skeleton and
the migration commands.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
