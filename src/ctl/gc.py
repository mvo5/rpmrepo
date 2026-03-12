"""rpmrepo - Garbage Collection

This module implements snapshot deletion using manifests for
deduplication tracking.  Each snapshot stores a manifest at
``data/manifest/{snapshot_full_id}`` listing all checksums it
references (one per line).

When deleting a snapshot, ``delete_snapshot`` reads all remaining
manifests to determine which checksums are still referenced.
Checksums unique to the deleted snapshot are orphaned and their
data blobs are removed.
"""

# pylint: disable=duplicate-code,invalid-name,too-few-public-methods

import concurrent.futures
import sys
import threading
import time

import boto3


BUCKET = "rpmrepo-storage"

# Thread-local storage for per-thread S3 clients.
_thread_local = threading.local()


def _s3c():
    """Return a per-thread S3 client."""

    if not hasattr(_thread_local, "s3c"):
        _thread_local.s3c = boto3.client("s3")
    return _thread_local.s3c


def _paginate_prefix(s3c, prefix):
    """Yield all object keys under *prefix*."""

    paginator = s3c.get_paginator("list_objects_v2")
    for page in paginator.paginate(
        Bucket=BUCKET,
        Prefix=prefix,
        PaginationConfig={"PageSize": 1000},
    ):
        for obj in page.get("Contents", []):
            yield obj["Key"]


def _progress(msg):
    sys.stdout.write(f"\r  {msg}")
    sys.stdout.flush()


def _elapsed(t0):
    secs = time.monotonic() - t0
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _run_concurrent(items, func, n_total, label, workers=50):
    """Run *func* on each item concurrently with progress reporting.

    *func(item)* is called in a thread pool.  Returns a list of results.
    """

    t0 = time.monotonic()
    counter = [0]
    lock = threading.Lock()
    results = []

    def _wrapper(item):
        result = func(item)
        with lock:
            results.append(result)
            counter[0] += 1
            if counter[0] % 500 == 0:
                _progress(f"[{counter[0]}/{n_total}] {_elapsed(t0)}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_wrapper, item) for item in items]
        for fut in concurrent.futures.as_completed(futs):
            fut.result()

    print(f"\n  {label}: {n_total} items in {_elapsed(t0)}")
    return results


def _read_manifest(s3c, snapshot_full_id):
    """Read a snapshot manifest and return its set of checksums."""

    key = f"data/manifest/{snapshot_full_id}"
    resp = s3c.get_object(Bucket=BUCKET, Key=key)
    body = resp["Body"].read().decode().strip()
    if not body:
        return set()
    return set(body.split("\n"))


def _scan_checksums_from_refs(s3c, snapshot_full_id, workers=50):
    """Fallback: scan refs via head_object when no manifest exists.

    Uses concurrent threads to speed up the head_object calls.
    """

    ref_prefix = f"data/ref/{snapshot_full_id}/"
    print(f"  Listing refs ...")
    keys = list(_paginate_prefix(s3c, ref_prefix))
    n_total = len(keys)
    print(f"  {n_total} refs to scan ({workers} workers)")

    checksums = set()
    lock = threading.Lock()

    def _fetch(key):
        resp = _s3c().head_object(Bucket=BUCKET, Key=key)
        checksum = resp.get("Metadata", {}).get("rpmrepo-checksum")
        if checksum:
            with lock:
                checksums.add(checksum)

    _run_concurrent(keys, _fetch, n_total, "Scanned refs", workers=workers)
    return checksums


def _get_checksums(s3c, snapshot_full_id):
    """Get checksums for a snapshot from manifest, falling back to refs."""

    try:
        return _read_manifest(s3c, snapshot_full_id)
    except s3c.exceptions.NoSuchKey:
        print(f"  No manifest for {snapshot_full_id}, scanning refs ...")
        checksums = _scan_checksums_from_refs(s3c, snapshot_full_id)

        # Write the manifest so we don't have to scan again.
        if checksums:
            manifest_key = f"data/manifest/{snapshot_full_id}"
            print(f"  Writing manifest {manifest_key}")
            s3c.put_object(
                Bucket=BUCKET,
                Key=manifest_key,
                Body="\n".join(sorted(checksums)),
            )

        return checksums


def _discover_snapshots(s3c):
    """Return list of all snapshot full IDs from thread markers."""

    snapshots = []
    for key in _paginate_prefix(s3c, "data/thread/"):
        parts = key.split("/")
        if len(parts) != 4 or parts[2] == "meta":
            continue
        snapshots.append(parts[3])
    return sorted(snapshots)


def delete_snapshot(snapshot_full_id, storage, platform_id, *, dry_run=False):
    """Delete a snapshot and garbage-collect orphaned data blobs.

    Reads manifests from all snapshots to determine which checksums
    are still referenced.  Blobs only referenced by the deleted
    snapshot are removed.
    """

    s3c = boto3.client("s3")

    # Derive the thread (snapshot-id without the date suffix).
    parts = snapshot_full_id.rsplit("-", 1)
    thread_id = parts[0]

    # 1. Get checksums for the snapshot being deleted.
    print(f"Reading checksums for {snapshot_full_id} ...")
    delete_checksums = _get_checksums(s3c, snapshot_full_id)
    print(f"  {len(delete_checksums)} unique checksums")

    # 2. Read all other manifests to find which checksums are still needed.
    print("Reading manifests for remaining snapshots ...")
    all_snapshots = _discover_snapshots(s3c)
    other_snapshots = [s for s in all_snapshots if s != snapshot_full_id]
    print(f"  {len(other_snapshots)} other snapshots")

    referenced = set()
    for idx, snap in enumerate(other_snapshots, 1):
        print(f"  [{idx}/{len(other_snapshots)}] {snap} ...", end=" ")
        sys.stdout.flush()
        checksums = _get_checksums(s3c, snap)
        referenced |= checksums
        print(f"{len(checksums)} checksums")

    # 3. Determine orphaned checksums.
    orphaned = delete_checksums - referenced
    print(f"\n  {len(orphaned)} orphaned / {len(delete_checksums)} total")

    if dry_run:
        print("Dry run, not deleting anything.")
        return

    # 4. Delete orphaned data blobs.
    if orphaned:
        def _del_blob(checksum):
            _s3c().delete_object(
                Bucket=BUCKET,
                Key=f"data/{storage}/{platform_id}/{checksum}",
            )

        print("Deleting orphaned blobs ...")
        _run_concurrent(
            sorted(orphaned), _del_blob, len(orphaned), "Deleted blobs")

    # 5. Delete all ref objects for this snapshot.
    ref_prefix = f"data/ref/{snapshot_full_id}/"
    print("Listing refs ...")
    ref_keys = list(_paginate_prefix(s3c, ref_prefix))

    def _del_ref(key):
        _s3c().delete_object(Bucket=BUCKET, Key=key)

    print("Deleting refs ...")
    _run_concurrent(ref_keys, _del_ref, len(ref_keys), "Deleted refs")

    # 6. Delete manifest.
    manifest_key = f"data/manifest/{snapshot_full_id}"
    print(f"Deleting manifest {manifest_key}")
    s3c.delete_object(Bucket=BUCKET, Key=manifest_key)

    # 7. Delete thread marker.
    thread_key = f"data/thread/{thread_id}/{snapshot_full_id}"
    print(f"Deleting thread marker {thread_key}")
    s3c.delete_object(Bucket=BUCKET, Key=thread_key)

    # 8. Invalidate enumerate cache.
    print("Invalidating enumerate cache")
    s3c.delete_object(Bucket=BUCKET, Key="data/thread/meta/cache.json")

    print(f"Snapshot {snapshot_full_id} deleted.")


def build_manifest_local(cache_root, snapshot_full_id, *, dry_run=False):
    """Build a manifest from the local index/snapshot cache directory.

    Reads checksums from the local ``index/snapshot/`` files (which
    contain the checksum as their content) and uploads the manifest
    to S3.  Much faster than scanning refs via S3 head_object calls.

    *cache_root* is the top-level cache dir (same as --cache).
    The snapshot-id (without date suffix) is derived from
    *snapshot_full_id* to locate the cache subdirectory.
    """

    import os

    # e.g. "f44-x86_64-branched" from "f44-x86_64-branched-20260312"
    snapshot_id = snapshot_full_id.rsplit("-", 1)[0]
    cache_dir = os.path.join(cache_root, snapshot_id)
    snapshot_dir = os.path.join(cache_dir, "index", "snapshot")

    if not os.path.isdir(snapshot_dir):
        print(f"Error: {snapshot_dir} does not exist")
        return

    # Verify the local cache actually belongs to the requested snapshot.
    marker_path = os.path.join(cache_dir, "conf", "snapshot-id")
    try:
        with open(marker_path, "r") as filp:
            cached_id = filp.read().strip()
        if cached_id != snapshot_full_id:
            print(f"Error: local cache belongs to {cached_id}, "
                  f"not {snapshot_full_id}")
            return
    except FileNotFoundError:
        print(f"Warning: no snapshot-id marker in cache "
              f"(missing {marker_path})")
        print(f"  Cannot verify cache matches {snapshot_full_id}. "
              f"Skipping.")
        return

    print(f"Reading checksums from {snapshot_dir} ...")
    checksums = set()
    for level, _, entries in os.walk(snapshot_dir):
        for entry in entries:
            with open(os.path.join(level, entry), "r") as filp:
                checksum = filp.read().strip()
                if checksum:
                    checksums.add(checksum)

    print(f"  {len(checksums)} unique checksums")

    if dry_run:
        print("Dry run, not uploading.")
        return

    s3c = boto3.client("s3")
    manifest_key = f"data/manifest/{snapshot_full_id}"
    print(f"Uploading manifest {manifest_key} ...")
    s3c.put_object(
        Bucket=BUCKET,
        Key=manifest_key,
        Body="\n".join(sorted(checksums)),
    )
    print("Done.")
