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
context.URL = URL;
context.window = { location: { origin: 'https://filaman.example' } };
context.printerFilter = { value: 'all' };
vm.runInContext(slice('const safeHttpUrl =', 'const requestJson =')
  + slice('const colorHex =', 'const finiteNumber =')
  + slice('const imageMetadata =', '// Keep at most one gallery page')
  + slice('const bindImageFallbacks =', 'const colorPill =')
  + slice('const renderSlot =', 'const inventoryPercent =')
  + slice('const renderInventoryCard =', 'const setButtonLoading =')
  + ';globalThis.renderInventory = renderInventory; globalThis.renderAms = renderAms;', context);
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

// Exercise the shared selection through both views, including the AMS image count.
context.t = (key, params) => key === 'slot.image' ? `Image: ${params.provider}` : key;
const nativeImage = 'https://example.com/filament.jpg';
const catalogImage = 'https://example.com/catalog.jpg';
const legacyImage = 'https://example.com/legacy.jpg';
const slotImage = 'https://example.com/slot.jpg';
const blackSpool = {
  id: 4,
  filament: {
    designation: 'Black',
    material_type: 'PLA',
    manufacturer: { name: 'ELEGOO' },
    shop_url: 'https://example.com/filament',
    image_url: nativeImage,
  },
};

const renderImages = (spool, slotFields = {}) => {
  context.inventory.items = [spool];
  context.renderInventory();
  const inventory = context.content.innerHTML;
  context.amsState = [{
    printer: { id: 1, name: 'Bambu Lab H2S' },
    health: {
      connected: true,
      slots: [{ present: true, slot_index: '1-1', spool_id: spool.id, ...slotFields }],
    },
    spools: new Map([[spool.id, spool]]),
  }];
  context.renderAms();
  return { inventory, ams: context.content.innerHTML, summary: context.summary.innerHTML };
};

const native = renderImages(blackSpool);
for (const view of [native.inventory, native.ams]) {
  assert.ok(view.includes(`src="${nativeImage}"`), 'native filament image appears in both views');
}
assert.match(native.summary, /ams\.catalogImages<\/span><strong class="metric-value">1<\/strong>/);
assert.match(native.ams, /Image: common\.catalog<\/a>/, 'native image has a generic source label');

const catalog = renderImages({
  ...blackSpool,
  filament: { ...blackSpool.filament, custom_fields: {
    filament_image_url: catalogImage, bambu_shop_image_url: legacyImage,
  } },
}, { shop_image_url: slotImage });
assert.ok(catalog.inventory.includes(`src="${catalogImage}"`), 'catalog metadata takes precedence');
assert.ok(catalog.ams.includes(`src="${slotImage}"`), 'AMS slot image keeps precedence');
assert.match(catalog.ams, /Image: Bambu Lab<\/a>/, 'AMS slot image keeps its provider label');

const legacy = renderImages({
  ...blackSpool,
  filament: { ...blackSpool.filament, custom_fields: {
    filament_image_url: 'javascript:alert(1)', bambu_shop_image_url: legacyImage,
  } },
});
for (const view of [legacy.inventory, legacy.ams]) {
  assert.ok(view.includes(`src="${legacyImage}"`), 'valid legacy URL can replace an invalid catalog URL');
}

const invalid = renderImages({
  ...blackSpool, filament: { ...blackSpool.filament, image_url: 'data:image/png,invalid' },
});
for (const view of [invalid.inventory, invalid.ams]) {
  assert.doesNotMatch(view, /<img class="shop-image"/);
  assert.match(view, /class="spool-art"/);
}
assert.match(invalid.summary, /ams\.catalogImages<\/span><strong class="metric-value">0<\/strong>/);
console.log('Plugin gallery uses native filament images and preserves catalog precedence: passed');

// Restore stored assignments only when the live driver has no assignment to report.
vm.runInContext(slice('const canonicalSlotIndex =', 'const loadAms =') + ';globalThis.loadAmsSlots = loadAmsSlots;', context);
const presentSlot = { slot_index: '0-0', present: true, tray_type: 'PLA', tray_info_idx: 'PLA_CUSTOM', tray_color: '161616FF' };
const savedSlot = {
  is_active: true,
  slot_no: 0,
  custom_fields: { slot_index: '0-0' },
  assignment: { present: true, spool_id: 4, tray_type: 'PLA', tray_info_idx: 'PLA_CUSTOM', tray_color: '000000' },
};
let reads = 0;
context.requestJson = async (url) => {
  assert.equal(url, '/api/v1/printers/1/slots');
  reads++;
  return [savedSlot];
};

const restoredSlots = await context.loadAmsSlots(1, { connected: true, slots: [presentSlot] });
assert.equal(restoredSlots[0].spool_id, 4);
context.amsState = [{ ...context.amsState[0], health: { connected: true, slots: restoredSlots }, spools: new Map([[4, blackSpool]]) }];
context.renderAms();
assert.ok(context.content.innerHTML.includes(`src="${nativeImage}"`), 'restored A1 shows the native filament photo');

for (const [connected, slot] of [
  [true, { ...presentSlot, spool_id: 9 }],
  [true, { ...presentSlot, spool_id: null }],
  [true, { ...presentSlot, present: false }],
  [false, presentSlot],
]) {
  const projected = await context.loadAmsSlots(1, { connected, slots: [slot] });
  assert.equal(projected[0], slot, 'live assignments, explicit unlink, empty, and offline slots are preserved');
}
assert.equal(reads, 1, 'only an occupied, connected slot without a live assignment needs a lookup');
const moved = { ...presentSlot, tray_info_idx: 'GFA00' };
const movedSlots = await context.loadAmsSlots(1, { connected: true, slots: [moved] });
assert.equal(movedSlots[0], moved, 'different live filament metadata rejects the stored assignment');
context.requestJson = async () => [{ ...savedSlot, custom_fields: { slot_index: '255-255' } }];
const external = { ...presentSlot, slot_index: '255-1' };
assert.equal((await context.loadAmsSlots(1, { connected: true, slots: [external] }))[0].spool_id, 4);
context.requestJson = async () => { throw new Error('unavailable'); };
assert.equal((await context.loadAmsSlots(1, { connected: true, slots: [presentSlot] }))[0], presentSlot);
console.log('AMS gallery restores stored spool links after a driver restart: passed');
