"""Shared infrastructure used across multiple alfred tools.

Currently hosts the ``schedule`` module — a clock-aligned scheduling
abstraction used by brief, janitor deep sweep, and distiller deep/
consolidation passes so heavy daily work lands overnight instead of
drifting with daemon restarts.
"""
