#!/usr/bin/env node
/**
 * GhostBus live-demo wrapper.
 *
 * Runs the REAL GhostBus relay (src/http-server.mjs from the ghostbus
 * package, unmodified) as a subprocess on a private port, registers four
 * agents on it (nova = coordinator, echo = worker, audience = observer,
 * guest = the watch page's posting identity), drives a legible work cycle
 * over the relay's real REST API, and serves a public watch page on
 * WEB_PORT. Everything else on WEB_PORT is proxied through to the relay,
 * so the page talks to the genuine /api/* surface.
 *
 * Token enforcement is ON in the relay (GHOSTBUS_REQUIRE_TOKENS=1). The
 * audience and guest tokens are embedded in the watch page: this is a
 * throwaway demo workspace with generated credentials and no real data.
 * Watching and posting are separate identities on purpose: "audience"
 * only ever observes, "guest" only ever posts tasks.
 */
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const GB_DIR = process.env.GHOSTBUS_DIR || '/app/ghostbus';
const RELAY_PORT = Number(process.env.RELAY_PORT || 8377);
const WEB_PORT = Number(process.env.WEB_PORT || 8080);
const BUILD = process.env.DEMO_BUILD || 'dev';
const RELAY = `http://127.0.0.1:${RELAY_PORT}`;
const CYCLE_MS = 30_000;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log('[demo]', ...a);

// ---------------------------------------------------------------- relay boot
const relayProc = spawn(
  'node',
  [path.join(GB_DIR, 'src/http-server.mjs'), '--port', String(RELAY_PORT), '--store', ':memory:', '--name', 'GhostBus Live Demo'],
  {
    env: {
      ...process.env,
      GHOSTBUS_REQUIRE_TOKENS: '1',
      GHOSTBUS_RATE_LIMIT_PER_MIN: '3000',
    },
    stdio: 'inherit',
  },
);
relayProc.on('exit', (code) => {
  log(`relay exited (${code}) — exiting so the platform restarts the demo clean`);
  process.exit(1);
});

// ------------------------------------------------------------------ api help
async function api(pathname, { method = 'GET', token = null, body = null } = {}) {
  const res = await fetch(RELAY + pathname, {
    method,
    headers: {
      'content-type': 'application/json',
      ...(token ? { 'x-agent-token': token } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let json;
  try { json = JSON.parse(text); } catch { json = { raw: text }; }
  if (!res.ok) throw new Error(`${method} ${pathname} -> ${res.status}: ${text.slice(0, 200)}`);
  return json;
}

async function waitForRelay() {
  for (let i = 0; i < 60; i++) {
    try {
      const h = await api('/health');
      if (h && h.ok) return h;
    } catch { /* not up yet */ }
    await sleep(500);
  }
  throw new Error('relay did not come up');
}

// ------------------------------------------------------------------- agents
const agents = {}; // name -> token
let relayVersion = 'unknown';
let cycleCount = 0;
let bootedAt = new Date().toISOString();

async function register(name, role, capabilities) {
  const out = await api('/api/agents', { method: 'POST', body: { agent: name, role, capabilities } });
  if (!out.token) throw new Error(`registration for ${name} returned no token`);
  agents[name] = out.token;
  log(`registered ${name} (${role})`);
}

// ------------------------------------------------------------------- driver
const WORK = [
  { title: 'Tidy the shared project notes', body: 'Bring the shared notes up to date so the next agent starts from the current picture, not a stale one.', result: 'Notes tidied: three sections updated, two stale ones retired.' },
  { title: 'Run a relay health check', body: 'Verify the bus end to end: presence, messaging, task flow, and the event log.', result: 'Relay healthy: presence, messaging, tasks, and event log all green.' },
  { title: 'Draft the 0.6.0 release blurb', body: 'One short paragraph on what 0.6.0 changes: DM confidentiality, corrupt-store safety, quotas, real HTTP statuses.', result: 'Blurb drafted and shared with the workspace.' },
  { title: 'Summarize the recent event log', body: 'Read the provenance log and post a short summary of what the team has done lately.', result: 'Summary posted from the provenance log — every step accounted for.' },
  { title: 'Prepare the demo board', body: 'Make sure the board shows the current agents, open work, and latest results before visitors arrive.', result: 'Board refreshed: agents, tasks, and results all current.' },
  { title: 'Check the task queue for stale claims', body: 'Look for claimed tasks whose lease may have lapsed and return them to the queue if so.', result: 'Queue checked: no stale claims holding work hostage.' },
];
const PROGRESS = [
  'Picked it up — starting now.',
  'Working on it — no blockers so far.',
  'Halfway there, everything checks out.',
  'Wrapping up the last details.',
];
const SECRETS = ['MANGO', 'VELVET', 'COPPER', 'JUNIPER', 'SABLE', 'HARBOUR', 'TUNDRA', 'MARBLE'];

async function broadcast(from, body) {
  return api('/api/messages', { method: 'POST', token: agents[from], body: { agent: from, to: '*', body, channel: 'general' } });
}

// ------------------------------------------------------- visitor task queue
// The watch page posts REAL tasks to the bus as the "guest" agent. A
// poller watches for them and the driver works them BEFORE the next
// canned item: echo claims the visitor's task, comments on it, and
// completes it with a result that quotes the visitor's own words.
const visitorSeen = new Set();
const visitorQueue = [];

async function pollVisitorTasks() {
  for (;;) {
    try {
      const d = await api('/api/tasks');
      for (const t of d.tasks || []) {
        if (t.from === 'guest' && t.status === 'queued' && !visitorSeen.has(t.id)) {
          visitorSeen.add(t.id);
          visitorQueue.push(t.id);
          log(`visitor task spotted: #${t.id} "${t.title}"`);
        }
      }
    } catch { /* transient — try again next tick */ }
    await sleep(2500);
  }
}

async function nextVisitorTask() {
  while (visitorQueue.length) {
    const id = visitorQueue.shift();
    try {
      const d = await api('/api/tasks');
      const t = (d.tasks || []).find((x) => x.id === id);
      if (t && t.status === 'queued') return t;
    } catch { /* fall through and try the next queued id */ }
  }
  return null;
}

async function workVisitorTask(t) {
  const title = t.title;
  await broadcast('nova', `📥 A visitor just posted a task from the watch page: "${title}". It jumps the queue — echo is taking it next, ahead of the routine work.`);
  await sleep(2500);
  await api(`/api/tasks/${t.id}/claim`, { method: 'POST', token: agents.echo, body: { agent: 'echo' } });
  await sleep(2500);
  await api(`/api/tasks/${t.id}/comment`, { method: 'POST', token: agents.echo, body: { agent: 'echo', body: `Picked up the visitor's task — working on "${title}" now.` } });
  await sleep(3500);
  await api(`/api/tasks/${t.id}/comment`, { method: 'POST', token: agents.echo, body: { agent: 'echo', body: 'Still on it. A visitor-posted task gets the same full lifecycle as any work on this bus: claimed, commented, completed, and recorded in the log.' } });
  await sleep(3500);
  await api(`/api/tasks/${t.id}/complete`, { method: 'POST', token: agents.echo, body: { agent: 'echo', result: `Done — "${title}" was claimed, worked, and completed by echo through the real relay API, at a visitor's request from the watch page.` } });
  await sleep(2000);
  await broadcast('nova', `Visitor task #${t.id} complete: "${title}". The form on the watch page stays open — post another any time.`);
}

async function driverLoop() {
  let n = 0;
  for (;;) {
    // Visitor tasks outrank the canned rotation — check before anything.
    const vt = await nextVisitorTask().catch(() => null);
    if (vt) {
      try { await workVisitorTask(vt); } catch (e) { log('visitor task error:', e.message); }
      continue;
    }
    n += 1;
    cycleCount = n;
    const work = WORK[(n - 1) % WORK.length];
    try {
      await api('/api/heartbeat', { method: 'POST', token: agents.nova, body: { agent: 'nova' } }).catch(() => {});
      await api('/api/heartbeat', { method: 'POST', token: agents.echo, body: { agent: 'echo' } }).catch(() => {});

      // One more check before committing to the canned item — a task a
      // visitor posted during the heartbeats still goes first.
      const late = await nextVisitorTask().catch(() => null);
      if (late) {
        try { await workVisitorTask(late); } catch (e) { log('visitor task error:', e.message); }
      }

      await broadcast('nova', `Cycle ${n}: new work dropping — "${work.title}". Posting it to the board now.`);
      await sleep(3000);
      const task = await api('/api/tasks', { method: 'POST', token: agents.nova, body: { agent: 'nova', title: work.title, body: work.body, assignee: 'echo' } });
      const tid = task.task ? task.task.id : task.id;
      await sleep(3000);
      await api(`/api/tasks/${tid}/claim`, { method: 'POST', token: agents.echo, body: { agent: 'echo' } });
      await sleep(3000);
      await api(`/api/tasks/${tid}/comment`, { method: 'POST', token: agents.echo, body: { agent: 'echo', body: PROGRESS[(n - 1) % PROGRESS.length] } });

      if (n % 3 === 0) {
        // The 0.6.0 moment: a real direct message between nova and echo.
        // Its content goes ONLY to echo. The audience stream never carries
        // it; the broadcast below announces the fact, never the content.
        const code = `${SECRETS[(n / 3 - 1) % SECRETS.length]}-${100 + ((n * 37) % 900)}`;
        await sleep(2500);
        await api('/api/messages', { method: 'POST', token: agents.nova, body: { agent: 'nova', to: 'echo', body: `Side channel, just us: the rendezvous code for this round is ${code}. Confirm when you've got it.` } });
        await sleep(1500);
        await api('/api/messages', { method: 'POST', token: agents.echo, body: { agent: 'echo', to: 'nova', body: 'Got it — code confirmed on my side. Saying no more out loud.' } });
        await sleep(1500);
        await broadcast('nova', '🔒 A private exchange just passed between nova and echo. You are watching as "audience": GhostBus 0.6.0 keeps direct messages out of your stream entirely, so you can see that it happened — never what it said.');
      }

      await sleep(4000);
      await api(`/api/tasks/${tid}/comment`, { method: 'POST', token: agents.echo, body: { agent: 'echo', body: PROGRESS[(n + 1) % PROGRESS.length] } });
      await sleep(4000);
      await api(`/api/tasks/${tid}/complete`, { method: 'POST', token: agents.echo, body: { agent: 'echo', result: work.result } });
      await sleep(2500);
      await broadcast('nova', `Done: "${work.title}" is complete. Result is recorded on task #${tid} — check the task list. Next cycle starts shortly.`);
    } catch (e) {
      log('cycle error:', e.message);
    }
    // Interruptible tail sleep: a visitor task cuts the wait short.
    const tail = CYCLE_MS - 24_000 > 0 ? CYCLE_MS - 24_000 : 3000;
    for (let waited = 0; waited < tail; waited += 1000) {
      if (visitorQueue.length) break;
      await sleep(Math.min(1000, tail - waited));
    }
  }
}

// ---------------------------------------------------------------- web server
const pageTemplate = fs.readFileSync(path.join(HERE, 'watch.html'), 'utf8');

function renderPage() {
  return pageTemplate
    .replaceAll('__AUDIENCE_TOKEN__', agents.audience || '')
    .replaceAll('__GUEST_TOKEN__', agents.guest || '')
    .replaceAll('__BUILD__', BUILD)
    .replaceAll('__VERSION__', relayVersion);
}

const web = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (req.method === 'GET' && (url.pathname === '/' || url.pathname === '/watch')) {
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' });
    return res.end(renderPage());
  }
  if (req.method === 'GET' && url.pathname === '/version') {
    res.writeHead(200, { 'content-type': 'application/json' });
    return res.end(JSON.stringify({
      app: 'ghostbus-demo',
      ghostbus: relayVersion,
      build: BUILD,
      bootedAt,
      cycles: cycleCount,
      relay: 'live',
    }));
  }
  // Everything else proxies to the real relay, untouched (SSE included).
  const upstream = http.request(
    { host: '127.0.0.1', port: RELAY_PORT, path: req.url, method: req.method, headers: req.headers },
    (up) => {
      res.writeHead(up.statusCode || 502, up.headers);
      up.pipe(res);
    },
  );
  upstream.on('error', () => {
    try { res.writeHead(502, { 'content-type': 'application/json' }); res.end(JSON.stringify({ error: 'relay not ready yet — retry in a few seconds' })); } catch { /* already sent */ }
  });
  req.pipe(upstream);
});

// Boot fully BEFORE binding the web port: the relay must be healthy and
// all three agents registered, so the first page a visitor gets already
// carries a working audience token and the real relay version. The
// platform's startup timeout covers this (boot takes a couple of seconds).
(async () => {
  try {
    const health = await waitForRelay();
    relayVersion = health.version || 'unknown';
    bootedAt = new Date().toISOString();
    await register('nova', 'coordinator — finds work, posts tasks, reports back', ['planning', 'coordination']);
    await register('echo', 'worker — claims tasks and does them', ['execution', 'reporting']);
    await register('audience', 'observer — watching the demo live', ['watching']);
    await register('guest', 'visitor — posts tasks from the watch page', ['posting']);
    await broadcast('nova', 'The GhostBus demo floor is open. nova (coordinator) and echo (worker) will run a work cycle about every 30 seconds: a task is posted, claimed, worked, and completed — all through the real relay API. Visitors: the form on the watch page posts a real task as "guest", and echo takes those first.');
    web.listen(WEB_PORT, () => log(`watch page on :${WEB_PORT} (build ${BUILD}), relay v${relayVersion}`));
    log('driver starting');
    driverLoop().catch((e) => log('driver died:', e));
    pollVisitorTasks().catch((e) => log('visitor poller died:', e));
  } catch (e) {
    log('boot failed:', e.message);
    process.exit(1);
  }
})();
