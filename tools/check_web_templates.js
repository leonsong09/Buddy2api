// Dev-only check for the hand-written web UI:
//   1. every `template:` literal in web/index.html must compile with the vendored
//      Vue (compiler + runtime) build, so a template typo fails here instead of in
//      the browser;
//   2. every inline <script> in the page must be syntactically valid JavaScript
//      (`new Function(source)` parses the body without ever running it).
//
// The vendored build is the browser (compiler + runtime) global bundle, so it
// expects a DOM for entity decoding and a global `Vue` identifier. We provide a
// tiny shim instead of pulling in a full DOM library.
//
// Removed pass: this tool used to also diff each template's `_ctx` references
// against the keys its component returned and print `bindings bad=`. That check
// could never fail: the vendored prod build's `Vue.compile()` returns a render
// function, not an object exposing `.code`, so the extracted reference set was
// always empty and the line printed `bindings bad=0` no matter what -- the
// `credit is not a function` crash in the account list walked straight through it.
// Pass (2) below keeps what that pass did verify for real (the app script has to be
// parseable) without pretending to compare bindings.
const fs = require('fs');
const path = require('path');

const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0' };
function decodeEntities(text) {
  return String(text).replace(/&(#x?[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);/g, (whole, name) => {
    if (name[0] === '#') {
      const hex = name[1] === 'x' || name[1] === 'X';
      const code = parseInt(hex ? name.slice(2) : name.slice(1), hex ? 16 : 10);
      return Number.isFinite(code) ? String.fromCodePoint(code) : whole;
    }
    return Object.prototype.hasOwnProperty.call(ENTITIES, name) ? ENTITIES[name] : whole;
  });
}

function makeElement() {
  let inner = '';
  const el = {
    get innerHTML() { return inner; },
    set innerHTML(v) {
      inner = String(v);
      // Vue's browser entity decoder writes `<div foo="...">` and then reads the
      // attribute back off the first child, so mirror just that shape.
      const attr = /^<div foo="([\s\S]*)">$/.exec(inner);
      el.children = attr ? [{ getAttribute: (n) => (n === 'foo' ? decodeEntities(attr[1]) : null) }] : [];
    },
    get textContent() { return decodeEntities(inner); },
    set textContent(v) { inner = String(v); },
    getAttribute: () => null,
    setAttribute() {},
    appendChild() {},
    children: [],
    childNodes: [],
    style: {},
    dataset: {},
    classList: { add() {}, remove() {} },
  };
  return el;
}

global.window = global;
global.document = {
  createElement: () => makeElement(),
  createElementNS: () => makeElement(),
  createTextNode: (t) => ({ textContent: String(t) }),
  querySelector: () => null,
  querySelectorAll: () => [],
  body: makeElement(),
  head: makeElement(),
  documentElement: makeElement(),
};

const vendor = path.join(__dirname, '..', 'web', 'vendor', 'vue.global.prod.min.js');
const src = fs.readFileSync(vendor, 'utf8');
// The minified build references the bare identifier `Vue` internally, so it must
// also be reachable as a global once the factory returns.
globalThis.Vue = global.window.Vue = new Function('window', 'document', src + '\n;return Vue;')(global.window, global.document);

const htmlPath = process.argv[2] || path.join(__dirname, '..', 'web', 'index.html');
const html = fs.readFileSync(htmlPath, 'utf8');

// ---------------------------------------------------------------- templates --
// Every `template:` literal must compile.
const re = /template:`([\s\S]*?)`\s*\}\)/g;
let m;
let i = 0;
let bad = 0;
while ((m = re.exec(html))) {
  i += 1;
  try {
    const result = globalThis.Vue.compile(m[1]);
    const errors = (result && result.errors) || [];
    if (errors.length) {
      bad += 1;
      console.log('template #' + i + ' errors: ' + errors.map(String).join(' | '));
    }
  } catch (e) {
    bad += 1;
    console.log('template #' + i + ' threw: ' + e.message);
  }
}
console.log('templates=' + i + ' bad=' + bad);

// ---------------------------------------------------------- inline scripts ---
// Every inline <script> must parse. `new Function` only parses the body -- it is
// never invoked, so this cannot run page code.
const scripts = [...html.matchAll(/<script(?![^>]*src=)[^>]*>([\s\S]*?)<\/script>/g)].map((s) => s[1]);
let parseBad = 0;
scripts.forEach((code, idx) => {
  try {
    new Function(code);
  } catch (e) {
    parseBad += 1;
    console.log('script #' + (idx + 1) + ' parse error: ' + e.message);
  }
});
console.log('scripts=' + scripts.length + ' parse bad=' + parseBad);

process.exit(bad || parseBad ? 1 : 0);
