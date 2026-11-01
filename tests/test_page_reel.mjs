import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

// The shipped page is standalone HTML; execute its pure data adapters unchanged.
const page = readFileSync(new URL('../bambulab/page.html', import.meta.url), 'utf8');
const slice = (start, end) => page.slice(page.indexOf(start), page.indexOf(end, page.indexOf(start)));
const context = { Number, Math };
vm.createContext(context);
vm.runInContext(slice('const finiteNumber =', 'const imageMetadata =') + slice('const inventoryPercent =', 'const renderInventoryCard =') + ';globalThis.percent = inventoryPercent; globalThis.data = reelData;', context);
const spool = { remaining_weight_g: 500, filament: { raw_material_weight_g: 1000, color_mode: 'multi', multi_color_style: 'gradient', colors: [{ position: 1, color: { hex_code: '#0000FF80' } }, { position: 0, color: { hex_code: '#FF8800' } }] } };
assert.equal(context.percent(spool), 50);
assert.equal(context.percent({ remaining_weight_g: null, filament: { raw_material_weight_g: 1000 } }), null);
assert.equal(context.percent({ ...spool, metrics: { remaining_percent: null } }), null);
assert.equal(context.percent({ ...spool, metrics: { remaining_percent: 0 } }), 0);
const data = context.data(spool);
assert.equal(data.fraction, .5);
assert.equal(data.mode, 'gradient');
assert.deepEqual(Array.from(data.colors), ['#FF8800', '#0000FF80']);
assert.equal(context.data(null, { present: false }).fraction, 0);
assert.equal(context.data(null, { present: true, remain: null }).fraction, null);
for (const remain of [-1, 101, Infinity, NaN, undefined, '', false]) {
  assert.equal(context.data(null, { present: true, remain }).fraction, null, `unknown AMS estimate ${remain} must not become an empty or full amount`);
}
for (const [remain, fraction] of [[0, 0], ['0', 0], [25, .25], [100, 1]]) {
  assert.equal(context.data(null, { present: true, remain }).fraction, fraction);
}
assert.equal(context.data(null, { present: false, remain: -1 }).fraction, 0);
assert.equal(context.data(null, { present: true, remain: 25, tray_color: 'AABBCC80' }).colors[0], '#AABBCC80');
assert.ok(page.includes('include_metrics'));
console.log('Plugin reel adapters: passed');
const legacySingle = { ...spool, filament: { color_mode: 'single', colors: [{ position: 0, color: { hex_code: 'legacy invalid' } }, { position: 1, color: { hex_code: '#FF0000' } }] } };
assert.deepEqual(Array.from(context.data(legacySingle).colors), []);

let builds = 0;
context.renderSpoolReel = () => `<svg id="reel-${++builds}"></svg>`;
context.escapeHtml = (value) => String(value);
context.t = (key) => key;
vm.runInContext('const reelCache = new Map();' + slice('const spoolFallback =', 'const bindImageFallbacks =') + ';globalThis.fallback = spoolFallback;', context);
const firstCard = context.fallback('#FF8800', false, data, 'inventory:1');
assert.equal(context.fallback('#FF8800', false, data, 'inventory:1'), firstCard, 'unchanged cards reuse their reel');
assert.notEqual(context.fallback('#FF8800', false, data, 'inventory:2'), firstCard, 'different cards keep unique SVG IDs');
assert.notEqual(context.fallback('#FF8800', false, { ...data, fraction: .25 }, 'inventory:1'), firstCard, 'weight changes regenerate the reel');
for (let id = 3; id < 70; id++) context.fallback('#FF8800', false, data, `inventory:${id}`);
const beforeEvicted = builds;
context.fallback('#FF8800', false, data, 'inventory:2');
assert.equal(builds, beforeEvicted + 1, 'paging evicts old cards rather than growing memory indefinitely');
context.renderSpoolReel = null;
assert.match(context.fallback('#FF8800'), /class="spool-art"/, 'older core retains its fallback');
console.log('Plugin reel caching and compatibility: passed');

// The page delegates color semantics to the core swatch helper, like filament/spool heroes.
let pillInput;
context.buildFilamentSwatchStyle = (value) => { pillInput = value; return 'background-image: linear-gradient(90deg, #FF8800, rgba(0, 0, 255, 0.5))'; };
vm.runInContext(slice('const colorPill =', 'const renderSlot =') + ';globalThis.pill = colorPill;', context);
assert.match(context.pill(data, '#FF8800'), /class="color-pill"/);
assert.deepEqual(Array.from(pillInput.colors, item => item.color.hex_code), ['#FF8800', '#0000FF80']);
assert.equal(pillInput.multi_color_style, 'gradient');
context.buildFilamentSwatchStyle = null;
assert.match(context.pill(data, '#FF8800'), /background:#FF8800/, 'old core gets a larger solid pill');
console.log('Plugin shared color pills: passed');

const themeEvents = {};
const themeDocument = { documentElement: { dataset: {} } };
let savedTheme = 'brand';
const themeContext = { document: themeDocument, localStorage: { getItem: key => key === 'theme' ? savedTheme : 'en' }, navigator: { language: 'en' }, window: { addEventListener: (event, listener) => { themeEvents[event] = listener; } } };
vm.runInNewContext(page.match(/<script>([\s\S]*?)<\/script>/)[1], themeContext);
assert.equal(themeDocument.documentElement.dataset.theme, 'brand');
savedTheme = 'light';
themeEvents.storage({ key: 'theme' });
assert.equal(themeDocument.documentElement.dataset.theme, 'light', 'gallery follows sidebar changes without a reload');
savedTheme = 'dark';
themeEvents.storage({ key: 'unrelated' });
assert.equal(themeDocument.documentElement.dataset.theme, 'light');
themeEvents.storage({ key: 'theme' });
assert.equal(themeDocument.documentElement.dataset.theme, 'dark');
savedTheme = null;
themeEvents.storage({ key: null });
assert.equal(themeDocument.documentElement.dataset.theme, 'brand', 'clearing theme restores the default');
console.log('Plugin live theme synchronization: passed');

// Execute both real renderers: nominal new-spool weight must reach cards and totals.
context.inventory = { items: [], total: 0, page: 1, pageSize: 48, manufacturers: [], statuses: new Map() };
context.content = { innerHTML: '', querySelectorAll: () => [] };
context.summary = { innerHTML: '' };
context.document = { getElementById: () => null };
vm.runInContext(slice('const safeHttpUrl =', 'const requestJson =')
  + slice('const colorHex =', 'const finiteNumber =')
  + slice('const imageMetadata =', 'const productCode =')
  + slice('const bindImageFallbacks =', 'const colorPill =')
  + slice('const renderInventoryCard =', 'const setButtonLoading =')
  + ';globalThis.renderInventory = renderInventory;', context);
for (const [weight, metrics, card, total] of [
  [null, { effective_remaining_weight_g: 1000, remaining_percent: 100 }, '1000 g', '1.0 kg'],
  [500, { effective_remaining_weight_g: 0, remaining_percent: 0 }, '0 g', '0.0 kg'],
  [500, { effective_remaining_weight_g: null, remaining_percent: null }, '—', '0.0 kg'],
  [500, undefined, '500 g', '0.5 kg'],
]) {
  context.inventory.items = [{ id: 1, remaining_weight_g: weight, metrics }];
  context.inventory.total = 1;
  context.renderInventory();
  assert.ok(context.content.innerHTML.includes(`<strong>${card}</strong>`), `card shows ${card}`);
  assert.ok(context.summary.innerHTML.includes(`>${total}</strong>`), `summary shows ${total}`);
}
console.log('Plugin gallery cards and total use effective remaining weight: passed');
