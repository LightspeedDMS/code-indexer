"""Fleet Migration (Story #1458, Epic #1454).

A one-time, serialized (one repo at a time), crash-restartable background
job that consolidates a golden repo's MUTABLE BASE CLONE in place: every
semantic collection's sharded ``vector_*.json`` layout is converted to a
consolidated ``chunks.db`` (Story #1456's engine), and every in-repo
temporal quarter-shard/monolith is bootstrapped to the sister location
(completing Story #1457's AC11), as ONE ordered per-repo job under a single
held write lock.
"""
