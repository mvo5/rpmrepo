"""rpmrepo - Snapshot RPM Repository

This module implements the snapshot pipeline that pulls an RPM repository,
indexes it, and pushes it to remote storage.
"""

# pylint: disable=duplicate-code,invalid-name,too-few-public-methods

import datetime
import json
import os
import re

import boto3
import botocore.exceptions

from . import index, pull, push


# Maps the tag portion of a Fedora snapshot-id (e.g. "branched", "rawhide",
# "fedora") to the release identifier that appears in the koji compose URL
# layout. "branched" and "fedora" both correspond to the numbered release;
# "rawhide" is symbolic. Other tags (e.g. "updates-released") have no
# direct equivalent in the compose URL surface and are skipped.
_FEDORA_TAG_RELEASE = {
    "rawhide": "rawhide",
    "branched": "_NUMERIC_",
    "fedora": "_NUMERIC_",
}


def _fedora_release(platform_id, snapshot_id):
    """Derive (release, arch, tag) from a Fedora snapshot config, or None."""

    m = re.match(r"^f(\d+)$", platform_id)
    if not m:
        return None
    rest = snapshot_id.removeprefix(f"{platform_id}-")
    if rest == snapshot_id or "-" not in rest:
        return None
    arch, tag = rest.split("-", 1)
    target = _FEDORA_TAG_RELEASE.get(tag)
    if target is None:
        return None
    release = m.group(1) if target == "_NUMERIC_" else target
    return release, arch, tag


class Snapshot:
    """Snapshot RPM repository"""

    def __init__(self, cache_root):
        self._cache_root = cache_root

    @staticmethod
    def _load_config(path):
        with open(path, "r", encoding="utf-8") as filp:
            return json.load(filp)

    @staticmethod
    def _snapshot_suffix(conf):
        if singleton := conf.get("singleton"):
            return f"-{singleton}"
        return f"-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d')}"

    @staticmethod
    def _snapshot_exists(snapshot_id, suffix):
        """Check whether a snapshot thread marker already exists in S3"""

        s3c = boto3.client("s3")
        key = f"data/thread/{snapshot_id}/{snapshot_id}{suffix}"
        try:
            s3c.head_object(Bucket="rpmrepo-storage", Key=key)
            return True
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            raise

    def run_one(self, path):
        """Run snapshot for a single repo config file"""

        conf = self._load_config(path)
        suffix = self._snapshot_suffix(conf)

        platform_id = conf["platform-id"]
        base_url = conf["base-url"]
        snapshot_id = conf["snapshot-id"]
        storage = conf["storage"]

        if self._snapshot_exists(snapshot_id, suffix):
            print(f"Snapshot {snapshot_id}{suffix} exists already, skipping")
            return

        # Derive a stable cache identifier from the snapshot-id so the
        # dnf cache is reused across runs of the same repo config.
        cache = os.path.join(self._cache_root, snapshot_id)
        os.makedirs(cache, exist_ok=True)
        print("LocalIdentifier:", snapshot_id)
        print("LocalCache:", cache)

        print(f"Pulling {snapshot_id} from {base_url}...")
        with pull.Pull(cache, platform_id, base_url) as cmd:
            cmd.pull()

        print(f"Indexing {snapshot_id}...")
        with index.Index(cache) as cmd:
            cmd.index()

        snapshot_full_id = f"{snapshot_id}{suffix}"

        print(f"Pushing {snapshot_full_id}...")
        with push.Push(cache) as cmd:
            cmd.push_data_s3(storage, platform_id)
            cmd.push_snapshot_s3(snapshot_id, suffix)

        # Record which snapshot this cache belongs to, so that
        # build-manifest can verify it is reading the right data.
        with open(os.path.join(cache, "conf", "snapshot-id"), "w") as filp:
            filp.write(snapshot_full_id)

        # Recognised Fedora snapshots publish a release pointer the cloudflare
        # worker reads to serve the koji-compose URL surface (used by mkosi's
        # Snapshot= setting). Derived from platform-id + snapshot-id, so no
        # extra config in the per-repo JSON is needed.
        if derived := _fedora_release(platform_id, snapshot_id):
            release, arch, tag = derived
            self._publish_release_pointer(
                release, platform_id, arch, tag, suffix,
            )

        print(f"Snapshot {snapshot_full_id} complete.")

    @staticmethod
    def _publish_release_pointer(release, platform_id, arch, tag, suffix):
        """Write data/latest/fedora-<release>.json describing the latest snapshot."""

        date = suffix.lstrip("-")
        body = json.dumps({
            "platform": platform_id,
            "tag": tag,
            "arch": arch,
            "date": date,
        })
        key = f"data/latest/fedora-{release}.json"
        print(f"Publishing release pointer {key} -> {body}")
        boto3.client("s3").put_object(
            Bucket="rpmrepo-storage",
            Key=key,
            Body=body.encode(),
            ContentType="application/json",
        )
