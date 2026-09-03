"""
Google Drive calls are I/O-bound (waiting on network), so plain threads
give real concurrency here despite the GIL -- no need for multiprocessing.
This is what makes checking free space across 10 drives, or uploading
several files at once, fast instead of sequential.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from . import config


def parallel_map(fn, items: list) -> list:
    """Run fn(item) for every item in items concurrently, preserving order."""
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(config.MAX_WORKERS, len(items))) as pool:
        return list(pool.map(fn, items))


def parallel_dict(fn, keyed_items: dict) -> dict:
    """Run fn(value) for every value in a dict concurrently, keeping keys."""
    keys = list(keyed_items.keys())
    values = [keyed_items[k] for k in keys]
    results = parallel_map(fn, values)
    return dict(zip(keys, results))
