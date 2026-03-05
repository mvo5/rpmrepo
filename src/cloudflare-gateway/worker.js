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
 *   /robots.txt
 *   /                -> redirect to documentation
 */

const DOCUMENTATION_URL = "https://osbuild.org/docs/developer-guide/projects/rpmrepo/";

const STORAGE_URLS = {
  "public": "https://pub-33e1ee802b2b44d9ba7e030178878f69.r2.dev/data/public",
};

const ROBOTS_TXT = "User-agent: *\nDisallow: /\n";

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

	return null;
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

		return errorResponse(400);
	},
};
