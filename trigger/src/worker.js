/**
 * Fires the radar's watch workflow on a schedule GitHub will actually keep.
 *
 * GitHub treats `schedule:` as best effort and drops runs under load: a
 * fifteen-minute cron on this repo delivered one run in 2h41m.
 * `workflow_dispatch` is not throttled that way, so this Worker's cron calls
 * the dispatch API instead. The workflow itself is unchanged -- this only
 * decides when it runs.
 *
 * Secrets (wrangler secret put ...):
 *   GITHUB_TOKEN   fine-grained PAT, Actions: read and write, this repo only
 *   TRIGGER_KEY    optional; enables manual firing via GET /?key=...
 */

const API = "https://api.github.com";
const UA = "f500-swe-radar-trigger";

function ghHeaders(env) {
  return {
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": UA,
  };
}

/**
 * Is a run of this workflow already going?
 *
 * The workflow's concurrency group would queue a second run rather than run it
 * twice, but a queue that never drains is just a slow leak -- when a fetch
 * overruns the interval, skipping is the honest response.
 */
async function alreadyRunning(env) {
  const url =
    `${API}/repos/${env.REPO}/actions/workflows/${env.WORKFLOW}/runs` +
    `?per_page=10&exclude_pull_requests=true`;
  const r = await fetch(url, { headers: ghHeaders(env) });
  if (!r.ok) return false; // can't tell -> dispatch anyway, GitHub will queue
  const body = await r.json();
  return (body.workflow_runs || []).some(
    (run) => run.status === "in_progress" || run.status === "queued",
  );
}

async function dispatch(env) {
  const url =
    `${API}/repos/${env.REPO}/actions/workflows/${env.WORKFLOW}/dispatches`;
  const r = await fetch(url, {
    method: "POST",
    headers: { ...ghHeaders(env), "Content-Type": "application/json" },
    body: JSON.stringify({ ref: env.BRANCH || "main" }),
  });
  // A successful dispatch is 204 with no body.
  if (r.status === 204) return { ok: true, status: 204 };
  return { ok: false, status: r.status, detail: (await r.text()).slice(0, 300) };
}

async function run(env) {
  if (!env.GITHUB_TOKEN) {
    return { ok: false, skipped: "GITHUB_TOKEN is not set" };
  }
  if (await alreadyRunning(env)) {
    return { ok: true, skipped: "a run is already in progress or queued" };
  }
  return await dispatch(env);
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      run(env).then((res) => {
        // Shows up in `wrangler tail`; Workers have nowhere else to log.
        console.log(JSON.stringify({ at: new Date().toISOString(), ...res }));
      }),
    );
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    // Manual fire, off unless TRIGGER_KEY is set. Constant-time-ish compare is
    // overkill here, but an unguarded trigger endpoint is not something to
    // leave on the open internet.
    if (url.searchParams.has("key")) {
      const given = url.searchParams.get("key") || "";
      if (!env.TRIGGER_KEY || given !== env.TRIGGER_KEY) {
        return new Response("not authorized\n", { status: 403 });
      }
      const res = await run(env);
      return Response.json(res, { status: res.ok ? 200 : 502 });
    }

    return new Response(
      [
        "f500-swe-radar trigger",
        `repo:      ${env.REPO}`,
        `workflow:  ${env.WORKFLOW}`,
        `branch:    ${env.BRANCH || "main"}`,
        `token set: ${env.GITHUB_TOKEN ? "yes" : "NO - run wrangler secret put GITHUB_TOKEN"}`,
        "",
        "Cron fires the workflow; this page is only a health check.",
        "",
      ].join("\n"),
      { headers: { "content-type": "text/plain; charset=utf-8" } },
    );
  },
};
