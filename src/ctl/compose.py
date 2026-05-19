"""rpmrepo - Compose RPM Repository Snapshots

This module merges multiple existing snapshots into one composed snapshot with
freshly-generated repodata covering the union of their package sets. The RPM
payloads are referenced by their existing content-addressed blobs; only the
new repodata bytes get added to storage.

Reuses the same on-disk cache layout as snapshot/index/push so the existing
push.Push() can ship the result without any awareness that it came from a
compose rather than a fresh pull.
"""

# pylint: disable=duplicate-code,invalid-name,too-few-public-methods

import concurrent.futures
import contextlib
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import boto3

BUCKET = "rpmrepo-storage"

_thread_local = threading.local()


def _s3c():
    """Return a per-thread S3 client (boto3 clients aren't safe to share)."""

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


class Compose(contextlib.AbstractContextManager):
    """Compose multiple snapshots into one."""

    def __init__(self, cache, platform_id, storage, inputs):
        self._cache = cache
        self._platform = platform_id
        self._storage = storage
        self._inputs = list(inputs)
        self._path_conf = os.path.join(cache, "conf")
        self._path_data = os.path.join(cache, "index/data")
        self._path_index = os.path.join(cache, "index")
        self._path_snapshot = os.path.join(cache, "index/snapshot")

    def __exit__(self, exc_type, exc_value, exc_tb):
        pass

    @staticmethod
    def _checksum(filp):
        h = hashlib.sha256()
        for block in iter(lambda: filp.read(4096), b''):
            h.update(block)
        return "sha256-" + h.hexdigest()

    def _blob_key(self, checksum):
        return f"data/{self._storage}/{self._platform}/{checksum}"

    def compose(self):
        """Build the composed snapshot in the local cache."""

        if os.path.isdir(self._path_index):
            shutil.rmtree(self._path_index)
        os.makedirs(self._path_data, exist_ok=True)
        os.makedirs(self._path_snapshot, exist_ok=True)
        os.makedirs(self._path_conf, exist_ok=True)

        per_input_repodata = []
        for snap in self._inputs:
            print(f"Walking refs of {snap}...")
            prefix = f"data/ref/{snap}/"
            tmp = tempfile.mkdtemp(prefix=f"rpmrepo-compose-{snap}-")
            per_input_repodata.append(tmp)

            # First page through the listing serially (cheap) to collect all
            # ref keys. HEAD + download is what's slow, so that's what we
            # parallelize below.
            items = []
            paginator = _s3c().get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
                for obj in page.get("Contents", []):
                    rel = obj["Key"][len(prefix):]
                    items.append((obj["Key"], rel))

            n_total = len(items)
            counter = [0]
            lock = threading.Lock()
            t0 = time.monotonic()

            def _resolve(item, _tmp=tmp):
                key, rel = item
                head = _s3c().head_object(Bucket=BUCKET, Key=key)
                checksum = head["Metadata"]["rpmrepo-checksum"]

                if rel.startswith("repodata/"):
                    # Need actual bytes locally so mergerepo_c can parse them.
                    local = os.path.join(_tmp, rel)
                    os.makedirs(os.path.dirname(local), exist_ok=True)
                    _s3c().download_file(BUCKET, self._blob_key(checksum), local)
                else:
                    # Non-repodata: just re-publish the ref under the new
                    # snapshot pointing at the same existing content.
                    out = os.path.join(self._path_snapshot, rel)
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    # Last write wins on duplicate paths across inputs
                    # (e.g. comps.xml from both fedora and updates). The
                    # repodata merge re-derives the canonical set so a
                    # rare RPM-path collision is OK to overwrite.
                    with open(out, "wb") as filp:
                        filp.write(checksum.encode())

                with lock:
                    counter[0] += 1
                    if counter[0] % 500 == 0:
                        sys.stdout.write(
                            f"\r  [{counter[0]}/{n_total}] {_elapsed(t0)}")
                        sys.stdout.flush()

            workers = 20
            print(f"  Resolving {n_total} refs ({workers} workers)...")
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futs = [pool.submit(_resolve, item) for item in items]
                for fut in concurrent.futures.as_completed(futs):
                    fut.result()
            print(f"\n  Done: {n_total} refs in {_elapsed(t0)}")

        merged = tempfile.mkdtemp(prefix="rpmrepo-compose-merged-")
        # --omit-baseurl: keep package locations as relative hrefs without
        # tagging them with xml:base pointing at the input dirs. Without this,
        # mergerepo_c bakes file:///tmp/rpmrepo-compose-... absolute paths
        # into the merged primary.xml, which dnf then preferred over the
        # repo's baseurl and tried to fetch RPMs from /tmp (404).
        cmd = ["mergerepo_c", "--method=ts", "--omit-baseurl", "-o", merged]
        for d in per_input_repodata:
            cmd.extend(["--repo", d])
        print("Running mergerepo_c...")
        subprocess.check_call(cmd)

        # Content-address the merged repodata into the local data dir,
        # and write the parallel ref structure under index/snapshot/repodata/.
        out_repodata = os.path.join(self._path_snapshot, "repodata")
        os.makedirs(out_repodata, exist_ok=True)
        merged_repodata = os.path.join(merged, "repodata")
        for entry in sorted(os.listdir(merged_repodata)):
            local = os.path.join(merged_repodata, entry)
            with open(local, "rb") as filp:
                checksum = self._checksum(filp)
            dest = os.path.join(self._path_data, checksum)
            if not os.path.exists(dest):
                shutil.move(local, dest)
            with open(os.path.join(out_repodata, entry), "wb") as filp:
                filp.write(checksum.encode())

        for d in per_input_repodata:
            shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(merged, ignore_errors=True)

        with open(os.path.join(self._path_conf, "index.ok"), "wb"):
            pass

        print(f"Compose ready in {self._cache}")
