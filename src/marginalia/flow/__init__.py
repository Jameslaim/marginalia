"""Execution — the loops that turn decisions into stored videos.

`search` finds candidates and applies the free, deterministic gates; `discover`
runs the round loop that judges them and stores the keepers. Both reach the
network, the ledger and the disk, which is exactly why they are not in `brain/`.
"""
