"""rpmrepo - Push RPM Repository

This module implements the functions that push local RPM repository
snapshots to configured remote storage.
"""

# pylint: disable=duplicate-code,invalid-name,too-few-public-methods

import concurrent.futures
import contextlib
import os
import sys
import threading
import time

import boto3

BUCKET = "rpmrepo-storage"

_thread_local = threading.local()


def _s3c():
    """Return a per-thread S3 client."""

    if not hasattr(_thread_local, "s3c"):
        _thread_local.s3c = boto3.client("s3")
    return _thread_local.s3c


def _elapsed(t0):
    secs = time.monotonic() - t0
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


class Push(contextlib.AbstractContextManager):
    """Push RPM repository"""

    def __init__(self, cache):
        self._cache = cache
        self._path_conf = os.path.join(cache, "conf")
        self._path_data = os.path.join(cache, "index/data")
        self._path_snapshot = os.path.join(cache, "index/snapshot")

    def __exit__(self, exc_type, exc_value, exc_tb):
        pass

    def push_data_s3(self, storage, platform_id, workers=20):
        """Push data to S3"""

        assert os.access(os.path.join(self._path_conf, "index.ok"), os.R_OK)
        assert storage in ["public", "rhvpn"]

        # Collect all (local_path, s3_key) pairs.
        items = []
        for level, _, entries in os.walk(self._path_data):
            levelpath = os.path.relpath(level, self._path_data)
            if levelpath == ".":
                path = platform_id
            else:
                path = os.path.join(platform_id, levelpath)

            for entry in entries:
                key = f"data/{storage}/{path}/{entry}"
                local = os.path.join(level, entry)
                items.append((local, key))

        n_total = len(items)
        counter = [0, 0, 0]  # [done, touched, uploaded]
        lock = threading.Lock()
        t0 = time.monotonic()

        def _push_one(item):
            local, key = item

            try:
                _s3c().copy_object(
                    Bucket=BUCKET,
                    Key=key,
                    CopySource={"Bucket": BUCKET, "Key": key},
                    MetadataDirective="COPY",
                )
                touched = True
            except _s3c().exceptions.ClientError as e:
                if e.response["Error"]["Code"] not in ("404", "NoSuchKey"):
                    raise

                with open(local, "rb") as filp:
                    _s3c().upload_fileobj(filp, BUCKET, key)
                touched = False

            with lock:
                counter[0] += 1
                if touched:
                    counter[1] += 1
                else:
                    counter[2] += 1
                if counter[0] % 500 == 0:
                    sys.stdout.write(
                        f"\r  [{counter[0]}/{n_total}] {_elapsed(t0)} "
                        f"({counter[1]} touched, {counter[2]} uploaded)")
                    sys.stdout.flush()

        print(f"Pushing {n_total} data blobs ({workers} workers) ...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_push_one, item) for item in items]
            for fut in concurrent.futures.as_completed(futs):
                fut.result()

        print(f"\n  Done: {counter[1]} touched, {counter[2]} uploaded "
              f"in {_elapsed(t0)}")

    def push_snapshot_s3(self, snapshot_id, snapshot_suffix, workers=20):
        """Push snapshot to S3"""

        assert os.access(os.path.join(self._path_conf, "index.ok"), os.R_OK)

        # Collect all (s3_key, checksum) pairs and the checksums set.
        items = []
        for level, _subdirs, entries in os.walk(self._path_snapshot):
            levelpath = os.path.relpath(level, self._path_snapshot)
            if levelpath == ".":
                path = snapshot_id + snapshot_suffix
            else:
                path = os.path.join(snapshot_id + snapshot_suffix, levelpath)

            for entry in entries:
                with open(os.path.join(level, entry), "rb") as filp:
                    checksum = filp.read().decode()
                s3_key = f"data/ref/{path}/{entry}"
                items.append((s3_key, checksum))

        n_total = len(items)
        checksums = set()
        counter = [0]
        lock = threading.Lock()
        t0 = time.monotonic()

        def _push_ref(item):
            s3_key, checksum = item
            _s3c().put_object(
                Body=b"",
                Bucket=BUCKET,
                Key=s3_key,
                Metadata={"rpmrepo-checksum": checksum},
            )
            with lock:
                checksums.add(checksum)
                counter[0] += 1
                if counter[0] % 500 == 0:
                    sys.stdout.write(
                        f"\r  [{counter[0]}/{n_total}] {_elapsed(t0)}")
                    sys.stdout.flush()

        print(f"Pushing {n_total} snapshot refs ({workers} workers) ...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_push_ref, item) for item in items]
            for fut in concurrent.futures.as_completed(futs):
                fut.result()

        print(f"\n  Done: {n_total} refs in {_elapsed(t0)}")

        # Write manifest: one checksum per line.
        snapshot_full_id = f"{snapshot_id}{snapshot_suffix}"
        manifest_key = f"data/manifest/{snapshot_full_id}"
        print(f"Writing manifest {manifest_key} "
              f"({len(checksums)} unique checksums)")
        s3c = boto3.client("s3")
        s3c.put_object(
            Bucket=BUCKET,
            Key=manifest_key,
            Body="\n".join(sorted(checksums)),
        )

        s3c.put_object(
            Body=b"",
            Bucket=BUCKET,
            Key=f"data/thread/{snapshot_id}/{snapshot_id}{snapshot_suffix}",
        )
