"""Instructor daemon — polls vault records for ``alfred_instructions``
directives and executes them in-process via the Anthropic SDK.

Part of the 6-commit ``alfred_instructions`` watcher rollout. Commit 2
ships the module skeleton (config + state + utils); later commits add
the daemon, executor, SKILL file, CLI, orchestrator registration, and
health probe.
"""
