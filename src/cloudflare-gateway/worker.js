/**
 * RPMrepo - Cloudflare Worker Gateway
 *
 * This worker replaces the AWS Lambda gateway for RPM repository snapshot
 * serving on Cloudflare R2. It handles mirror requests by reading the
 * checksum metadata from ref objects in R2 and returning a redirect to the
 * actual data file.
 *
 * R2 Bucket Binding: This worker requires an R2 bucket binding named
 * "BUCKET" pointing to the rpmrepo-storage bucket. Configure this in
 * wrangler.toml or the Cloudflare dashboard.
 *
 * URL scheme:
 *   /v2/mirror/<storage>/<platform>/<snapshot>/<path...>
 *   /v2/enumerate[/<thread>]
 *   /compose/<release>/Fedora-<Release>-<snapshot>/compose/Everything/<arch>/os/<path...>
 *   /compose/<release>/latest-Fedora-<Release>/COMPOSE_ID
 *   /robots.txt
 *   /                -> redirect to documentation
 *
 * The /compose/... surface mimics the kojipkgs.fedoraproject.org layout so
 * that mkosi's Fedora Snapshot= setting works against this worker. <release>
 * is mapped to (platform, tag) by reading a pointer file written by the
 * snapshot/compose job (see loadReleasePointer) and translated internally
 * into the existing data/ref/<snapshot-id>/<path> R2 layout. The pointer
 * names the compose stream; clients can request any date that has a thread
 * marker, not just the pointer's current `date`.
 */

const DOCUMENTATION_URL = "https://osbuild.org/docs/developer-guide/projects/rpmrepo/";

const STORAGE_URLS = {
  "public": "https://pub-33e1ee802b2b44d9ba7e030178878f69.r2.dev/data/public",
};

const ROBOTS_TXT = "User-agent: *\nDisallow: /\n";

// Pointer file produced by the snapshot/compose job for each Fedora release.
// Body is JSON: {"platform": "f44", "tag": "compose", "date": "20260519"}.
// `tag` selects the stream (e.g. "compose" for ctl-compose merged snapshots,
// "branched" for pre-GA composes, "rawhide" for nightly). `date` is the most
// recent snapshot of that stream and is what the COMPOSE_ID endpoint serves
// so mkosi `latest-snapshot` finds it. Backend snapshot ids are constructed
// as f<platform>-<arch>-<tag>-<date_from_url>, so clients can pin to any past
// date that still has a thread marker, not just the pointer's current `date`.
function releasePointerKey(release) {
	return `data/latest/fedora-${release}.json`;
}

async function loadReleasePointer(bucket, release) {
	const obj = await bucket.get(releasePointerKey(release));
	if (!obj) return null;
	try {
		const data = JSON.parse(await obj.text());
		if (!data.platform || !data.tag || !data.date) return null;
		return data;
	} catch (_) {
		return null;
	}
}

function errorResponse(code) {
	return new Response(null, { status: code });
}

function redirectResponse(location) {
	return new Response(null, {
		status: 301,
		headers: { "Location": location },
	});
}

function successResponse(body) {
	return new Response(body || "", {
		status: 200,
		headers: { "Content-Type": "application/json" },
	});
}

/**
 * Parse the URL path into a command object.
 *
 * Returns null on invalid paths, or an object like:
 *   { mirror: { storage, platform, snapshot, path } }
 *   { enumerate: { thread? } }
 *   { redirect: { location } }
 */
function parseRequest(pathname) {
	// Strip leading slash and split
	const raw = pathname.replace(/^\/+/, "");
	if (!raw) return null;

	const segments = raw.split("/");

	// Reject empty segments (double slashes, trailing slashes)
	if (segments.some(s => s.length === 0)) return null;

	// Decode each segment
	const elements = segments.map(s => decodeURIComponent(s));

	// Handle versioned API prefix (v2/) or bare commands
	let command, args;
	if (elements[0] === "v2") {
		if (elements.length < 2) return null;
		command = elements[1];
		args = elements.slice(2);
	} else {
		command = elements[0];
		args = elements.slice(1);
	}

	if (command === "enumerate") {
		if (args.length === 0) return { enumerate: {} };
		if (args.length === 1) return { enumerate: { thread: args[0] } };
		return null;
	}

	if (command === "mirror") {
		if (args.length < 4) return null;
		return {
			mirror: {
				storage: args[0],
				platform: args[1],
				snapshot: args[2],
				path: args.slice(3).join("/"),
			},
		};
	}

	// koji-compose compatible surface used by mkosi's Snapshot= setting:
	//
	//   /compose/<release>/Fedora-<Release>-<snapshot>/compose/Everything/<arch>/os/<path...>
	//   /compose/<release>/latest-Fedora-<Release>/COMPOSE_ID
	if (elements[0] === "compose") {
		return parseComposeRequest(elements);
	}

	return null;
}

function parseComposeRequest(elements) {
	// elements[0] is "compose"
	if (elements.length < 3) return null;
	const release = elements[1];

	// latest-Fedora-<Release>/COMPOSE_ID
	if (elements.length === 4 &&
	    elements[2] === `latest-Fedora-${capitalize(release)}` &&
	    elements[3] === "COMPOSE_ID") {
		return { composeLatest: { release } };
	}

	// Fedora-<Release>-<snapshot>/compose/Everything/<arch>/os/<path...>
	if (elements.length < 8) return null;
	const dirName = elements[2];
	const prefix = `Fedora-${capitalize(release)}-`;
	if (!dirName.startsWith(prefix)) return null;
	const snapshot = dirName.slice(prefix.length);
	if (!snapshot) return null;
	if (elements[3] !== "compose") return null;
	if (elements[4] !== "Everything") return null;
	const arch = elements[5];
	if (elements[6] !== "os") return null;
	const path = elements.slice(7).join("/");
	if (!path) return null;

	return { compose: { release, snapshot, arch, path } };
}

function capitalize(s) {
	if (!s) return s;
	return s[0].toUpperCase() + s.slice(1);
}

/**
 * Handle mirror requests by reading ref object metadata from R2.
 */
async function handleMirror(bucket, { storage, platform, snapshot, path }) {
	const storageUrl = STORAGE_URLS[storage];
	if (!storageUrl) return errorResponse(406);

	const refKey = `data/ref/${snapshot}/${path}`;
	const obj = await bucket.head(refKey);

	if (!obj) return errorResponse(404);

	const checksum = (obj.customMetadata || {})["rpmrepo-checksum"];
	if (!checksum) return errorResponse(404);

	const destination = `${storageUrl}/${platform}/${checksum}`;
	return redirectResponse(destination);
}

/**
 * Handle a koji-compose-style mirror request.
 *
 * Translates the kojipkgs URL layout into our internal snapshot id and
 * defers to handleMirror() for the actual R2 lookup and redirect.
 */
async function handleCompose(bucket, { release, snapshot, arch, path }) {
	const ptr = await loadReleasePointer(bucket, release);
	if (!ptr) return errorResponse(404);

	return handleMirror(bucket, {
		storage: "public",
		platform: ptr.platform,
		snapshot: `${ptr.platform}-${arch}-${ptr.tag}-${snapshot}`,
		path,
	});
}

/**
 * Serve the COMPOSE_ID pointer for a release. Returns "Fedora-<Release>-<date>"
 * matching what kojipkgs.fedoraproject.org serves.
 */
async function handleComposeLatest(bucket, { release }) {
	const ptr = await loadReleasePointer(bucket, release);
	if (!ptr) return errorResponse(404);

	return new Response(`Fedora-${capitalize(release)}-${ptr.date}\n`, {
		status: 200,
		headers: { "Content-Type": "text/plain" },
	});
}

/**
 * Handle enumerate requests by serving the cached snapshot index.
 */
async function handleEnumerate(bucket, args) {
	// Try serving from the enumerate cache first
	const cacheKey = "data/thread/meta/cache.json";
	const cacheObj = await bucket.get(cacheKey);
	if (cacheObj) {
		return new Response(cacheObj.body, {
			status: 200,
			headers: { "Content-Type": "application/json" },
		});
	}

	// Fall back to listing thread objects
	const prefix = args.thread
		? `data/thread/${args.thread}/`
		: "data/thread/";

	const results = [];
	let cursor;

	do {
		const listed = await bucket.list({
			prefix: prefix,
			cursor: cursor,
		});

		for (const obj of listed.objects) {
			const key = obj.key.split("/").pop();
			if (key.length > 0) results.push(key);
		}

		cursor = listed.truncated ? listed.cursor : null;
	} while (cursor);

	results.sort();
	return successResponse(JSON.stringify(results));
}

export default {
	async fetch(request, env) {
		const url = new URL(request.url);
		const pathname = url.pathname;

		// Handle root redirect
		if (pathname === "/" || pathname === "") {
			return redirectResponse(DOCUMENTATION_URL);
		}

		// Handle robots.txt
		if (pathname === "/robots.txt") {
			return new Response(ROBOTS_TXT, {
				status: 200,
				headers: { "Content-Type": "text/plain" },
			});
		}

		const cmd = parseRequest(pathname);
		if (!cmd) return errorResponse(400);

		if (cmd.mirror) return handleMirror(env.BUCKET, cmd.mirror);
		if (cmd.enumerate) return handleEnumerate(env.BUCKET, cmd.enumerate);
		if (cmd.compose) return handleCompose(env.BUCKET, cmd.compose);
		if (cmd.composeLatest) return handleComposeLatest(env.BUCKET, cmd.composeLatest);

		return errorResponse(400);
	},
};
