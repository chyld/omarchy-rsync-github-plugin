"""File Vault's engine, split by concern. engine.py is the entry point.

    common   limits, Failure, and validation of every value from outside
    places   where File Vault keeps its data, the lock, and reset
    config   config.json, this machine, and vault definitions
    proc     running git and rsync: deadlines, output caps, readable errors
    plan     what a copy takes and skips (secrets included), and mirroring it
    sync     connect, copy, push, status and check against the local copy
    views    list and diff, shown in a pager

Modules refer to each other as `module.name`, never `from module import
name`, so a test can replace one function everywhere by patching it once.
"""
