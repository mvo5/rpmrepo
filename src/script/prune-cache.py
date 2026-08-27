#!/usr/bin/python3
"""Prune stale packages from an rpmrepo local pull-cache.

`dnf4 reposync` is invoked without `--delete`, so every package ever seen
by a repo config stays in `<cache>/<snapshot-id>/repo/` forever. Every
snapshot that referenced those packages has already been pushed to S3, so
the only reason to keep a local copy is to make the next reposync
incremental -- and that only needs the packages the *current* remote
metadata advertises.

This walks `repo/repodata/` to find the set of packages that are still
referenced, and deletes everything else. Dry-run unless --apply is given.

Note the `index/` directory is removed as well: its content-addressed
entries are hardlinks into `repo/` (see ctl/index.py), so unlinking a
package from `repo/` alone frees no space while the index still pins the
inode. `ctl index` rebuilds it from scratch on the next run anyway.
"""

import argparse
import gzip
import lzma
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET


# Sentinel added to the reference set when repomd.xml could not be parsed,
# telling the walk to leave repodata/ alone rather than delete all of it.
_KEEP_REPODATA = "\0keep-repodata"


def _open_metadata(path):
    """Return a decompressed stream for a repodata file."""

    with open(path, "rb") as filp:
        magic = filp.read(6)

    if magic.startswith(b"\x1f\x8b"):
        return gzip.open(path, "rb")
    if magic.startswith(b"\xfd7zXZ"):
        return lzma.open(path, "rb")
    if magic.startswith(b"\x28\xb5\x2f\xfd"):
        # zstd: prefer the module, fall back to the CLI.
        try:
            import zstandard
        except ImportError:
            proc = subprocess.Popen(
                ["zstd", "-dcq", path], stdout=subprocess.PIPE,
            )
            return proc.stdout
        return zstandard.ZstdDecompressor().stream_reader(open(path, "rb"))
    if magic.startswith(b"BZh"):
        import bz2
        return bz2.open(path, "rb")
    return open(path, "rb")


def _repomd_locations(path_repodata):
    """Yield the href of every file listed in repomd.xml."""

    try:
        tree = ET.parse(os.path.join(path_repodata, "repomd.xml"))
    except (ET.ParseError, OSError):
        # Truncated or missing repomd.xml: report nothing, which makes the
        # caller fall back to a listing scan and leaves repodata/ untouched.
        return
    for elem in tree.iter():
        if elem.tag.endswith("}location") or elem.tag == "location":
            href = elem.get("href")
            if href:
                yield href


def _primary_path(path_repodata):
    """Locate the primary.xml file inside a repodata directory."""

    for href in _repomd_locations(path_repodata):
        name = os.path.basename(href)
        if "primary.xml" in name:
            return os.path.join(path_repodata, name)

    # repomd.xml did not help (truncated metadata?); guess from the listing.
    for name in sorted(os.listdir(path_repodata)):
        if "primary.xml" in name:
            return os.path.join(path_repodata, name)
    return None


def _referenced_packages(path_repo):
    """Return the set of repo-relative paths referenced by the metadata."""

    path_repodata = os.path.join(path_repo, "repodata")
    primary = _primary_path(path_repodata)
    if primary is None or not os.path.exists(primary):
        raise RuntimeError(f"no primary.xml found in {path_repodata}")

    refs = set()
    stream = _open_metadata(primary)
    try:
        for _, elem in ET.iterparse(stream, events=("end",)):
            if elem.tag.endswith("}location") or elem.tag == "location":
                href = elem.get("href")
                if href:
                    refs.add(os.path.normpath(href))
                elem.clear()
    finally:
        stream.close()

    if not refs:
        raise RuntimeError(f"{primary} listed no packages")

    # `reposync --download-metadata` writes a fresh, checksum-named set of
    # metadata files on every run and leaves the previous set behind, so
    # repodata/ accumulates just like the package tree does. Keep only what
    # the current repomd.xml points at (plus repomd.xml and its signatures,
    # which are not self-referenced).
    repodata_refs = set()
    for href in _repomd_locations(path_repodata):
        repodata_refs.add(os.path.normpath(href))
    if repodata_refs:
        for name in ("repomd.xml", "repomd.xml.asc", "repomd.xml.key"):
            repodata_refs.add(os.path.join("repodata", name))
        refs |= repodata_refs
    else:
        # Nothing parsed out of repomd.xml -- do not touch repodata/ at all.
        refs.add(_KEEP_REPODATA)

    return refs


def _stale(path_repo, refs):
    """Yield (relpath, size) for every file in repo/ that is not referenced."""

    keep_repodata = _KEEP_REPODATA in refs

    for level, _, entries in os.walk(path_repo):
        rel_level = os.path.relpath(level, path_repo)
        is_repodata = (
            rel_level == "repodata"
            or rel_level.startswith("repodata" + os.sep)
        )
        if is_repodata and keep_repodata:
            continue
        for entry in entries:
            path = os.path.join(level, entry)
            rel = os.path.normpath(os.path.relpath(path, path_repo))
            if rel in refs:
                continue
            if os.path.islink(path):
                yield rel, 0
                continue
            yield rel, os.stat(path).st_size


def _prune_empty_dirs(path_repo):
    for level, _, _ in sorted(os.walk(path_repo, topdown=False)):
        if os.path.relpath(level, path_repo).startswith("repodata"):
            continue
        if level == path_repo:
            continue
        try:
            os.rmdir(level)
        except OSError:
            pass


def _human(size):
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            return f"{size:.1f}{unit}"
        size /= 1024
    return None


def prune(cache, apply_changes):
    """Prune one `<cache>/<snapshot-id>` directory."""

    path_repo = os.path.join(cache, "repo")
    if not os.path.isdir(path_repo):
        print(f"{cache}: no repo/ directory, skipping")
        return 0

    refs = _referenced_packages(path_repo)
    stale = list(_stale(path_repo, refs))
    total = sum(size for _, size in stale)

    print(
        f"{cache}: {len(refs)} referenced, "
        f"{len(stale)} stale ({_human(total)})"
    )
    if not stale:
        return 0

    if not apply_changes:
        for rel, size in sorted(stale, key=lambda i: -i[1])[:5]:
            print(f"    would delete {rel} ({_human(size)})")
        if len(stale) > 5:
            print(f"    ... and {len(stale) - 5} more")
        return total

    # The index hardlinks into repo/, so it has to go first or the inodes
    # stay alive. It is regenerated by `ctl index` on the next run.
    path_index = os.path.join(cache, "index")
    if os.path.isdir(path_index):
        print("    removing index/ (hardlinks pin the packages)")
        shutil.rmtree(path_index)
    try:
        os.unlink(os.path.join(cache, "conf", "index.ok"))
    except FileNotFoundError:
        pass

    for rel, _ in stale:
        os.unlink(os.path.join(path_repo, rel))
    _prune_empty_dirs(path_repo)
    print(f"    freed {_human(total)}")
    return total


def main(argv):
    parser = argparse.ArgumentParser(
        description="Prune stale packages from rpmrepo pull-caches",
    )
    parser.add_argument(
        "caches",
        help="Per-snapshot cache directories (e.g. /mnt/vol/rpmrepo/*)",
        metavar="PATH",
        nargs="+",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete; without this only report what would go",
    )
    args = parser.parse_args(argv[1:])

    total = 0
    for cache in args.caches:
        try:
            total += prune(cache.rstrip("/"), args.apply)
        except (RuntimeError, OSError, ET.ParseError) as e:
            print(f"{cache}: {e}", file=sys.stderr)

    verb = "freed" if args.apply else "reclaimable"
    print(f"total {verb}: {_human(total)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
