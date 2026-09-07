"""
This is the "you decide how to fetch/store across drives" piece.

Strategy on upload:
  1. Check free space on every connected drive IN PARALLEL (one query).
  2. For each file, if any single drive has room, use whichever has the
     MOST free space (spreads wear evenly instead of always filling #1).
  3. If no single drive fits, split the file into chunks sized to each
     drive's remaining free space.
  4. Flatten every chunk from every file in the batch into one big list
     and upload ALL of them concurrently in a single thread pool -- this
     is what makes multi-file uploads fast: file A's chunk to drive 3
     and file B's chunk to drive 7 happen at the same time.

Every file/chunk is read straight off disk (via its file descriptor, at
whatever byte offset that chunk starts at) directly into the upload --
never fully read into a Python bytes object first. That's what keeps
peak memory bounded by drive_client.TRANSFER_CHUNK_SIZE regardless of
how large the file is; see upload_from_fd()'s docstring for the detail.

Download reverses step 4: every chunk of a file is fetched concurrently,
each written directly to its correct byte offset in a shared temp file
on disk (not into memory) -- so reassembling a large, many-chunk file
never holds more than one chunk's transfer buffer in memory either.
"""
from __future__ import annotations

import tempfile

from .drive_client import DriveClient
from .workers import parallel_dict, parallel_map


def get_free_space_by_account(clients: dict[str, DriveClient]) -> dict[str, int]:
    return parallel_dict(lambda c: c.get_free_space(), clients)


def get_storage_overview(clients: dict[str, DriveClient]) -> dict[str, dict]:
    """One parallel round of quota checks, used to render the pooled +
    per-drive gauges on the dashboard in a single pass."""
    return parallel_dict(lambda c: c.get_storage_info(), clients)


def plan_chunks(free_space: dict[str, int], size: int) -> list[tuple[str, int, int]]:
    """
    Pure planning, no network calls. Mutates free_space in place so
    callers can plan several files back-to-back against one snapshot.
    """
    single_fit = [acct for acct, free in free_space.items() if free >= size]
    if single_fit:
        best = max(single_fit, key=lambda a: free_space[a])
        free_space[best] -= size
        return [(best, 0, size)]

    ordered = sorted(free_space.items(), key=lambda kv: kv[1], reverse=True)
    total_available = sum(free for _, free in ordered)
    if total_available < size:
        raise ValueError(
            f"Not enough combined free space across connected drives "
            f"({total_available} bytes free, need {size})."
        )

    plan = []
    offset = 0
    for acct, free in ordered:
        if offset >= size:
            break
        chunk_size = min(free, size - offset)
        if chunk_size <= 0:
            continue
        plan.append((acct, offset, chunk_size))
        free_space[acct] -= chunk_size
        offset += chunk_size
    return plan


def upload_many(clients: dict[str, DriveClient], payloads: list[tuple[str, int, int]]) -> dict[str, list[dict]]:
    """
    payloads: [(filename, fd, size), ...] -- fd is an open, readable file
    descriptor (e.g. an UploadFile's own .file.fileno(), which Starlette
    has already spooled to a real temp file on disk for anything past a
    small in-memory threshold) positioned at the start of that file's
    content; size is its total byte length.

    Returns {filename: [chunk_record, ...]} once every chunk of every
    file has been uploaded. Never reads a whole file into memory -- see
    the module docstring.
    """
    free_space = get_free_space_by_account(clients)

    file_plans: dict[str, list[tuple[str, int, int]]] = {}
    fds: dict[str, int] = {}
    for filename, fd, size in payloads:
        file_plans[filename] = plan_chunks(free_space, size)
        fds[filename] = fd

    tasks = [
        (filename, acct, offset, chunk_size)
        for filename, _fd, _size in payloads
        for (acct, offset, chunk_size) in file_plans[filename]
    ]

    def do_task(task):
        filename, acct, offset, chunk_size = task
        is_split = len(file_plans[filename]) > 1
        chunk_name = f"{filename}.part{offset}" if is_split else filename
        # fresh_copy(): two chunks from different files can land on the
        # same drive and run concurrently -- each needs its own connection.
        file_id = clients[acct].fresh_copy().upload_from_fd(chunk_name, fds[filename], offset, chunk_size)
        return filename, {"account": acct, "file_id": file_id, "offset": offset, "size": chunk_size}

    grouped: dict[str, list[dict]] = {filename: [] for filename, _fd, _size in payloads}
    for filename, chunk in parallel_map(do_task, tasks):
        grouped[filename].append(chunk)
    return grouped


def download_with_chunks(clients: dict[str, DriveClient], chunks: list[dict]) -> str:
    """Fetches every chunk concurrently, each written directly to its
    correct offset in one shared temp file on disk. Returns that file's
    path -- the caller is responsible for deleting it once it's been
    streamed back to the client (main.py does this via a background
    task on the response)."""
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp_path = tmp.name
    dest_fd = tmp.fileno()

    def fetch(chunk):
        clients[chunk["account"]].fresh_copy().download_to_fd(chunk["file_id"], dest_fd, chunk["offset"])

    parallel_map(fetch, chunks)
    tmp.close()
    return tmp_path


def delete_chunks(clients: dict[str, DriveClient], chunks: list[dict]) -> None:
    parallel_map(lambda c: clients[c["account"]].fresh_copy().delete_file(c["file_id"]), chunks)
