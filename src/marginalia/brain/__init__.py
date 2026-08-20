"""Reasoning. Decisions in, no side effects out.

`brain/` reasons and returns decisions; `flow/` executes them. Nothing here
performs filesystem, database, network or provider access — it may call the LLM
abstraction and read `contracts`, and nothing else that touches the outside
world. That is what makes the parsers testable without a model and the whole
package testable without a machine.

Gains `territory.py` and `planner.py` later; that is why `judge.py` lives here
rather than at the top level.
"""
