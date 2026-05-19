"""rpmrepo - Command Line Interface

The `cli` module provides a command-line interface to the rpmrepo package. It
provides the most basic way to execute and interact with the rpmrepo functions.
"""

# pylint: disable=duplicate-code,invalid-name,too-few-public-methods

import argparse
import contextlib
import os
import re
import sys
import uuid

from . import compose, gc, index, pull, push, enumerate_cache, snapshot


class CliIndex:
    """Index Command"""

    def __init__(self, ctx):
        self._ctx = ctx

    def run(self):
        """Run index command"""

        self._ctx._create_local_cache()

        with index.Index(self._ctx.cache) as cmd:
            cmd.index()

        return 0


class CliPull:
    """Pull Command"""

    def __init__(self, ctx):
        self._ctx = ctx

    def run(self):
        """Run pull command"""

        self._ctx._create_local_cache()

        with pull.Pull(
                self._ctx.cache,
                self._ctx.args.platform_id,
                self._ctx.args.base_url,
            ) as cmd:
            cmd.pull()

        return 0


class CliPush:
    """Push Command"""

    def __init__(self, ctx):
        self._ctx = ctx

    def _parse_args(self):
        for entry in self._ctx.args.to:
            assert len(entry) == 3
            assert entry[0] in ["data", "snapshot"]
            if entry[0] == "data":
                assert entry[1] in ["public", "rhvpn"]

    def run(self):
        """Run push command"""

        self._ctx._create_local_cache()
        self._parse_args()

        with push.Push(self._ctx.cache) as cmd:
            for entry in self._ctx.args.to:
                if entry[0] == "data":
                    cmd.push_data_s3(entry[1], entry[2])
                elif entry[0] == "snapshot":
                    cmd.push_snapshot_s3(entry[1], entry[2])

        return 0

class CliEnumerateCache:
    """EnumerateCache command"""

    def __init__(self, ctx):
        self._ctx = ctx

    def run(self):
        """Run EnumerateCache command"""

        with enumerate_cache.EnumerateCache() as cmd:
            cmd.build()

        return 0

class CliSnapshot:
    """Snapshot Command"""

    def __init__(self, ctx):
        self._ctx = ctx

    def run(self):
        """Run snapshot command"""

        if self._ctx.args.compose and len(self._ctx.args.files) != 1:
            print("--compose requires exactly one repo file", file=sys.stderr)
            return 1

        cmd = snapshot.Snapshot(self._ctx.args.cache)
        result = None
        for path in self._ctx.args.files:
            result = cmd.run_one(path)

        if self._ctx.args.compose and result:
            self._run_compose(result)

        return 0

    def _run_compose(self, snap):
        """Compose the freshly-made snapshot with the bases from --compose."""

        # Derive output id from the fresh snapshot's arch + date.
        full = snap["snapshot_full_id"]
        m = re.match(r"^(f\d+)-([^-]+)-[^-]+(?:-[^-]+)*-(\d{8})$", full)
        if not m:
            print(
                f"Cannot derive compose output id from {full}; pass an "
                "explicit ctl compose invocation instead",
                file=sys.stderr,
            )
            return
        platform_id, arch, date = m.groups()
        output = f"{platform_id}-{arch}-{self._ctx.args.compose_tag}-{date}"

        inputs = list(self._ctx.args.compose) + [full]
        print(f"Composing {output} from {', '.join(inputs)}...")

        # Fresh local subdir so the compose doesn't collide with the cache
        # used by the snapshot step.
        compose_cache = os.path.join(self._ctx.args.cache, f"compose-{output}")
        os.makedirs(compose_cache, exist_ok=True)

        with compose.Compose(
                compose_cache, snap["platform_id"], snap["storage"], inputs,
        ) as cmd:
            cmd.compose()

        with push.Push(compose_cache) as cmd:
            cmd.push_data_s3(snap["storage"], snap["platform_id"])
            cmd.push_snapshot_s3(output, "")

        # Re-publish the release pointer to point at the compose, not the
        # raw updates snapshot. compose tag wins because that's what mkosi
        # Snapshot= should consume going forward.
        release = platform_id.removeprefix("f")
        snapshot.Snapshot._publish_release_pointer(
            release, snap["platform_id"], arch, self._ctx.args.compose_tag,
            f"-{date}",
        )


class CliCompose:
    """Compose Command — merge multiple snapshots into one with fresh repodata."""

    def __init__(self, ctx):
        self._ctx = ctx

    def run(self):
        """Run compose command"""

        self._ctx._create_local_cache()

        with compose.Compose(
                self._ctx.cache,
                self._ctx.args.platform_id,
                self._ctx.args.storage,
                self._ctx.args.inputs,
        ) as cmd:
            cmd.compose()

        with push.Push(self._ctx.cache) as cmd:
            cmd.push_data_s3(self._ctx.args.storage, self._ctx.args.platform_id)
            cmd.push_snapshot_s3(self._ctx.args.output, "")

        # Publish the release pointer so mkosi `latest-snapshot` sees this
        # compose. Output id is parsed as f<release>-<arch>-<tag>-<date>.
        # Format constraints match what snapshot._fedora_release expects.
        m = re.match(r"^f(\d+)-([^-]+)-([^-]+)-(\d{8})$", self._ctx.args.output)
        if m:
            release, arch, tag, date = m.groups()
            snapshot.Snapshot._publish_release_pointer(
                release, self._ctx.args.platform_id, arch, tag, f"-{date}",
            )

        return 0


class CliGc:
    """GC Command — delete a snapshot and clean up orphaned data"""

    def __init__(self, ctx):
        self._ctx = ctx

    def run(self):
        """Run gc command"""

        gc.delete_snapshot(
            self._ctx.args.snapshot,
            self._ctx.args.storage,
            self._ctx.args.platform_id,
            dry_run=self._ctx.args.dry_run,
        )
        return 0


class CliBuildManifest:
    """Build Manifest Command — build manifest from local cache"""

    def __init__(self, ctx):
        self._ctx = ctx

    def run(self):
        """Run build-manifest command"""

        gc.build_manifest_local(
            self._ctx.args.cache,
            self._ctx.args.snapshot,
            dry_run=self._ctx.args.dry_run,
        )
        return 0


class Cli(contextlib.AbstractContextManager):
    """RPMrepo Command Line Interface"""

    EXITCODE_INVALID_COMMAND = 1

    def __init__(self, argv):
        self.args = None
        self.cache = None
        self.local = None
        self._argv = argv
        self._exitstack = None
        self._parser = None

    def _parse_args(self):
        self._parser = argparse.ArgumentParser(
            add_help=True,
            allow_abbrev=False,
            argument_default=None,
            description="RPM Repository Snapshot Management",
            prog="rpmrepo",
        )
        self._parser.add_argument(
            "--cache",
            help="Path to cache-directory to use",
            metavar="PATH",
            required=True,
            type=os.path.abspath,
        )
        self._parser.add_argument(
            "--local",
            help="Name to use for local indexing",
            metavar="NAME",
            type=str,
        )

        cmd = self._parser.add_subparsers(
            dest="cmd",
            title="RPMrepo Commands",
        )

        _cmd_index = cmd.add_parser(
            "index",
            add_help=True,
            allow_abbrev=False,
            argument_default=None,
            description="Create index for an RPM repository",
            help="Create RPM repository index",
            prog=f"{self._parser.prog} index",
        )

        cmd_pull = cmd.add_parser(
            "pull",
            add_help=True,
            allow_abbrev=False,
            argument_default=None,
            description="Pull an RPM repository to local storage",
            help="Fetch a full RPM Repository",
            prog=f"{self._parser.prog} pull",
        )
        cmd_pull.add_argument(
            "--base-url",
            help="RPM repository base URL to fetch from",
            metavar="URL",
            required=True,
            type=str,
        )
        cmd_pull.add_argument(
            "--platform-id",
            help="RPM platform ID to use",
            metavar="ID",
            required=True,
            type=str,
        )

        cmd_push = cmd.add_parser(
            "push",
            add_help=True,
            allow_abbrev=False,
            argument_default=None,
            description="Push an RPM repository to remote storage",
            help="Push a full RPM Repository",
            prog=f"{self._parser.prog} push",
        )
        cmd_push.add_argument(
            "--to",
            action="append",
            default=[],
            help="Target to push to",
            metavar="DESC",
            nargs=3,
            type=str,
        )

        cmd_push = cmd.add_parser(
            "enumerate-cache",
            add_help=True,
            allow_abbrev=False,
            argument_default=None,
            description="Build the cache for enumerate",
            help="Build the cache for enumerate",
            prog=f"{self._parser.prog} enumerate-cache",
        )

        cmd_snapshot = cmd.add_parser(
            "snapshot",
            add_help=True,
            allow_abbrev=False,
            argument_default=None,
            description="Run the pull/index/push pipeline for a repo config",
            help="Run full snapshot from a repo JSON config with sensible defaults",
            prog=f"{self._parser.prog} snapshot",
        )
        cmd_snapshot.add_argument(
            "files",
            help="Path to repo JSON config file(s)",
            metavar="FILE",
            nargs="+",
            type=str,
        )
        cmd_snapshot.add_argument(
            "--compose",
            help="After snapshotting, compose the result with the given base "
                 "snapshot id(s). Repeat to merge several bases (e.g. GA "
                 "singleton + extras). Only valid with a single repo file.",
            action="append",
            metavar="SNAPSHOT_ID",
        )
        cmd_snapshot.add_argument(
            "--compose-tag",
            help="Tag used in the compose output id (default: compose). "
                 "Output is f<release>-<arch>-<compose-tag>-<date>.",
            default="compose",
            type=str,
        )

        cmd_compose = cmd.add_parser(
            "compose",
            add_help=True,
            allow_abbrev=False,
            argument_default=None,
            description="Merge several existing snapshots into one with fresh repodata",
            help="Compose snapshots into a single mergerepo'd snapshot",
            prog=f"{self._parser.prog} compose",
        )
        cmd_compose.add_argument(
            "--output",
            help="Output snapshot ID (e.g. f44-x86_64-compose-20260519)",
            required=True,
            type=str,
        )
        cmd_compose.add_argument(
            "--platform-id",
            help="Platform ID for output blobs (e.g. f44)",
            required=True,
            type=str,
        )
        cmd_compose.add_argument(
            "--storage",
            help="Storage tier (default: public)",
            default="public",
            type=str,
        )
        cmd_compose.add_argument(
            "inputs",
            help="Input snapshot full IDs (e.g. f44-x86_64-fedora-20260430)",
            metavar="SNAPSHOT",
            nargs="+",
            type=str,
        )

        cmd_gc = cmd.add_parser(
            "gc",
            add_help=True,
            allow_abbrev=False,
            argument_default=None,
            description="Delete a snapshot and garbage-collect orphaned data",
            help="Delete a snapshot and its orphaned data blobs",
            prog=f"{self._parser.prog} gc",
        )
        cmd_gc.add_argument(
            "snapshot",
            help="Full snapshot ID (e.g. f44-x86_64-branched-20260310)",
            type=str,
        )
        cmd_gc.add_argument(
            "--storage",
            help="Storage tier (default: public)",
            default="public",
            type=str,
        )
        cmd_gc.add_argument(
            "--platform-id",
            help="Platform ID (e.g. f44). Derived from snapshot ID if omitted.",
            type=str,
        )
        cmd_gc.add_argument(
            "--dry-run",
            help="Only show what would be deleted",
            action="store_true",
        )

        cmd_build_manifest = cmd.add_parser(
            "build-manifest",
            add_help=True,
            allow_abbrev=False,
            argument_default=None,
            description="Build a manifest from local index cache and upload to S3",
            help="Build manifest from local cache (fast bootstrap)",
            prog=f"{self._parser.prog} build-manifest",
        )
        cmd_build_manifest.add_argument(
            "snapshot",
            help="Full snapshot ID (e.g. f44-x86_64-branched-20260310)",
            type=str,
        )
        cmd_build_manifest.add_argument(
            "--dry-run",
            help="Only scan, do not upload",
            action="store_true",
        )

        return self._parser.parse_args(self._argv[1:])

    def _verify_args(self):
        if not self.args.cmd:
            print("No subcommand specified", file=sys.stderr)
            self._parser.print_help(file=sys.stderr)
            sys.exit(Cli.EXITCODE_INVALID_COMMAND)

    def _create_local_cache(self):
        """Create a UUID-based local cache directory.

        Used by low-level commands (index, pull, push) that operate
        on a single working directory.  Higher-level commands like
        snapshot manage their own cache layout.
        """

        self.local = self.args.local
        if not self.local:
            self.local = uuid.uuid4().hex

        print("LocalIdentifier:", self.local, file=sys.stdout)

        self.cache = os.path.join(self.args.cache, self.local)
        os.makedirs(self.cache, exist_ok=True)

        print("LocalCache:", self.cache, file=sys.stdout)

    def __enter__(self):
        self._exitstack = contextlib.ExitStack()
        with self._exitstack:
            self.args = self._parse_args()
            self._verify_args()

            self._exitstack = self._exitstack.pop_all()

        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self._exitstack.close()
        self._exitstack = None

    def run(self):
        """Execute selected commands"""

        if self.args.cmd == "index":
            ret = CliIndex(self).run()
        elif self.args.cmd == "pull":
            ret = CliPull(self).run()
        elif self.args.cmd == "push":
            ret = CliPush(self).run()
        elif self.args.cmd == "enumerate-cache":
            ret = CliEnumerateCache(self).run()
        elif self.args.cmd == "snapshot":
            ret = CliSnapshot(self).run()
        elif self.args.cmd == "compose":
            # Tell push to write the thread/manifest under the user-supplied
            # output id rather than deriving it from a repo config.
            self.args.snapshot_id = self.args.output
            ret = CliCompose(self).run()
        elif self.args.cmd == "gc":
            # Derive platform-id from snapshot if not given
            # e.g. "f44" from "f44-x86_64-branched-20260310"
            if not self.args.platform_id:
                self.args.platform_id = self.args.snapshot.split("-")[0]
            ret = CliGc(self).run()
        elif self.args.cmd == "build-manifest":
            ret = CliBuildManifest(self).run()
        else:
            raise RuntimeError("Command mismatch")

        return ret
