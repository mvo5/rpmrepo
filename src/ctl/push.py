"""rpmrepo - Push RPM Repository

This module implements the functions that push local RPM repository
snapshots to configured remote storage.
"""

# pylint: disable=duplicate-code,invalid-name,too-few-public-methods

import contextlib
import errno
import os

import boto3

from . import util


class Push(contextlib.AbstractContextManager):
    """Push RPM repository"""

    def __init__(self, cache):
        self._cache = cache
        self._path_conf = os.path.join(cache, "conf")
        self._path_data = os.path.join(cache, "index/data")
        self._path_snapshot = os.path.join(cache, "index/snapshot")

    def __exit__(self, exc_type, exc_value, exc_tb):
        pass

    def push_data_s3(self, storage, platform_id):
        """Push data to S3"""

        assert os.access(os.path.join(self._path_conf, "index.ok"), os.R_OK)
        assert storage in ["public", "rhvpn"]

        s3c = boto3.client("s3")

        n_total = 0
        for _, _, entries in os.walk(self._path_data):
            for entry in entries:
                n_total += 1

        i_total = 0
        for level, _, entries in os.walk(self._path_data):
            levelpath = os.path.relpath(level, self._path_data)
            if levelpath == ".":
                path = platform_id
            else:
                path = os.path.join(platform_id, levelpath)

            for entry in entries:
                i_total += 1

                print(f"[{i_total}/{n_total}] 'data/{storage}/{path}/{entry}'")

                with open(os.path.join(level, entry), "rb") as filp:
                    s3c.upload_fileobj(
                        filp,
                        "rpmrepo-storage",
                        f"data/{storage}/{path}/{entry}",
                    )

    def push_snapshot_s3(self, snapshot_id, snapshot_suffix):
        """Push snapshot to S3"""

        assert os.access(os.path.join(self._path_conf, "index.ok"), os.R_OK)

        s3c = boto3.client("s3")

        n_total = 0
        for _, _, entries in os.walk(self._path_snapshot):
            for entry in entries:
                n_total += 1

        i_total = 0
        for level, _subdirs, entries in os.walk(self._path_snapshot):
            levelpath = os.path.relpath(level, self._path_snapshot)
            if levelpath == ".":
                path = os.path.join(snapshot_id + snapshot_suffix)
            else:
                path = os.path.join(snapshot_id + snapshot_suffix, levelpath)

            for entry in entries:
                i_total += 1

                with open(os.path.join(level, entry), "rb") as filp:
                    checksum = filp.read().decode()

                print(f"[{i_total}/{n_total}] '{path}/{entry}' -> {checksum}")

                s3c.put_object(
                    Body=b"",
                    Bucket="rpmrepo-storage",
                    Key=f"data/ref/{path}/{entry}",
                    Metadata={"rpmrepo-checksum": checksum},
                )

        s3c.put_object(
            Body=b"",
            Bucket="rpmrepo-storage",
            Key=f"data/thread/{snapshot_id}/{snapshot_id}{snapshot_suffix}",
        )

    def push_data_dir(self, output):
        """Push data to a local directory"""

        assert os.access(os.path.join(self._path_conf, "index.ok"), os.R_OK)

        data_dir = os.path.join(output, "data")
        os.makedirs(data_dir, exist_ok=True)

        n_total = 0
        for _, _, entries in os.walk(self._path_data):
            for entry in entries:
                n_total += 1

        i_total = 0
        for level, _, entries in os.walk(self._path_data):
            for entry in entries:
                i_total += 1
                target = os.path.join(data_dir, entry)

                if os.path.exists(target):
                    print(f"[{i_total}/{n_total}] 'data/{entry}' (exists)")
                    continue

                print(f"[{i_total}/{n_total}] 'data/{entry}'")

                source = os.path.join(level, entry)
                os.link(source, target)

    def push_snapshot_dir(self, output, snapshot_id, snapshot_suffix):
        """Push snapshot to a local directory"""

        assert os.access(os.path.join(self._path_conf, "index.ok"), os.R_OK)

        data_dir = os.path.join(output, "data")

        n_total = 0
        for _, _, entries in os.walk(self._path_snapshot):
            for entry in entries:
                n_total += 1

        i_total = 0
        for level, _subdirs, entries in os.walk(self._path_snapshot):
            levelpath = os.path.relpath(level, self._path_snapshot)
            if levelpath == ".":
                path = snapshot_id + snapshot_suffix
            else:
                path = os.path.join(snapshot_id + snapshot_suffix, levelpath)

            target_dir = os.path.join(output, path)
            os.makedirs(target_dir, exist_ok=True)

            for entry in entries:
                i_total += 1

                with open(os.path.join(level, entry), "rb") as filp:
                    checksum = filp.read().decode()

                target = os.path.join(target_dir, entry)
                data_file = os.path.join(data_dir, checksum)

                print(f"[{i_total}/{n_total}] '{path}/{entry}' -> data/{checksum}")

                with util.suppress_oserror(errno.ENOENT):
                    os.unlink(target)
                os.link(data_file, target)

        thread_dir = os.path.join(output, "thread", snapshot_id)
        os.makedirs(thread_dir, exist_ok=True)
        thread_marker = os.path.join(thread_dir, snapshot_id + snapshot_suffix)
        with open(thread_marker, "a"):
            pass

    @staticmethod
    def gc_dir(output):
        """Remove unreferenced data files from a local directory"""

        data_dir = os.path.join(output, "data")
        n_deleted = 0
        n_kept = 0

        for entry in os.listdir(data_dir):
            path = os.path.join(data_dir, entry)
            if os.stat(path).st_nlink == 1:
                os.unlink(path)
                n_deleted += 1
            else:
                n_kept += 1

        print(f"GC: deleted {n_deleted}, kept {n_kept}")
