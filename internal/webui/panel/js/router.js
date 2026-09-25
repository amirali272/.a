/* Hash routing.
 *
 * The hash is used rather than the History API because the panel is served by
 * a Go mux that knows nothing about client-side paths: a deep link to
 * /panel/t/fr-relay would 404 on reload, and the fix is a catch-all rewrite.
 * #/t/fr-relay reloads correctly everywhere.
 *
 * Routes are patterns with :params — "#/t/:name/logs" — matched in order.
 */

const routes = [];
let current = null;
let onChange = () => { };

/* The last page that was not a dialog.
 *
 * Every dialog route draws a page underneath it, and that page used to be a
 * fixed default — the tunnels. So opening Health check from the overview drew
 * the tunnels behind it, and closing left you on the tunnels: you pressed the
 * cross on a dialog you opened from one page and arrived at another.
 *
 * Closing goes back to whatever this says instead. */
let home = '/';
export function setHome(path) { home = path; }
export function getHome() { return home; }

export function route(pattern, view) {
  const keys = [];
  const rx = new RegExp('^' + pattern
    .replace(/[.+?^${}()|[\]\\]/g, '\\$&')
    .replace(/:(\w+)/g, (_, k) => { keys.push(k); return '([^/]+)'; })
    .replace(/\*/g, '.*') + '$');
  routes.push({ rx, keys, view, pattern });
}

export function parse(hash = location.hash) {
  const raw = (hash || '#/').replace(/^#/, '') || '/';
  const [path, query = ''] = raw.split('?');
  return { path, query: new URLSearchParams(query) };
}

export function match(path) {
  for (const r of routes) {
    const m = path.match(r.rx);
    if (!m) continue;
    const params = {};
    r.keys.forEach((k, i) => { params[k] = decodeURIComponent(m[i + 1]); });
    return { ...r, params };
  }
  return null;
}

export function go(to, replace = false) {
  const hash = to.startsWith('#') ? to : '#' + to;
  if (location.hash === hash) { resolve(); return; }
  if (replace) history.replaceState(null, '', hash);
  else location.hash = hash;
}

/* The previous view is told to tear down before the next one renders, so a
   poller or an animation frame belonging to a screen nobody is looking at
   stops rather than accumulating. */
export function resolve() {
  const { path, query } = parse();
  const hit = match(path);
  if (!hit) { go('/', true); return; }
  if (current && current.teardown) current.teardown();
  current = { teardown: null };
  const ctx = { params: hit.params, query, path, setTeardown: fn => { current.teardown = fn; } };

  /* Clear the previous view's "enter" class BEFORE the new view renders, so
     the animation can play on the new root each time without re-triggering on
     a partial update. The view functions build their own DOM into #view; this
     just removes the class from whatever was there. */
  const host = document.getElementById('view');
  if (host) {
    for (const child of host.children) child.classList.remove('enter');
  }

  hit.view(ctx);

  /* Fade the new root in, once. The animation is declared in leather.css on
     `#view > .enter`; we add the class here after render so it plays exactly
     on a real navigation and never on an internal repaint (a store update
     replaces innerHTML, which would otherwise re-fire the animation every
     poll). Removed on animationend so a later paint cannot replay it. */
  if (host) {
    const root = host.firstElementChild;
    if (root) {
      root.classList.add('enter');
      root.addEventListener('animationend', () => root.classList.remove('enter'), { once: true });
    }
  }

  onChange(path, hit);
}

export function start(handler) {
  onChange = handler || onChange;
  window.addEventListener('hashchange', resolve);
  if (!location.hash) history.replaceState(null, '', '#/');
  resolve();
}