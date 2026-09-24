const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync('public/index.html', 'utf8').replace(/\r/g, '');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
let source = scripts[scripts.length - 1][1].replace(/loadCloudData\(\);\s*$/, '');

function classList() {
  const values = new Set();
  return {
    toggle(name, force) {
      const enabled = force === undefined ? !values.has(name) : !!force;
      enabled ? values.add(name) : values.delete(name);
      return enabled;
    },
    add(...names) { names.forEach(name => values.add(name)); },
    remove(...names) { names.forEach(name => values.delete(name)); },
    contains(name) { return values.has(name); },
  };
}

const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    id, classList: classList(), style: {}, value: '', textContent: '', innerHTML: '',
    addEventListener() {}, querySelectorAll() { return []; }, querySelector() { return null; },
    setAttribute() {}, appendChild() {},
  });
  return elements.get(id);
}

const storage = new Map();
let exportedBlob = null;
const ExcelJS = require('../public/lib/exceljs.min.js');
const context = {
  console, Intl, Date, Math, Number, String, Map, Set, Blob,
  localStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value) },
  document: {
    documentElement: { setAttribute(name, value) { this[name] = value; } },
    body: { classList: classList(), appendChild() {}, removeChild() {} },
    getElementById: element,
    querySelectorAll() { return []; },
    querySelector() { return null; },
    addEventListener() {},
    createElement() { const created = element(`created-${elements.size}`); created.click = () => {}; return created; },
  },
  window: { innerWidth: 1400, innerHeight: 900 },
  URL: { createObjectURL(blob) { exportedBlob = blob; return 'blob:test'; }, revokeObjectURL() {} },
  firebase: {}, ExcelJS,
};
vm.createContext(context);
vm.runInContext(source, context);

assert.equal(context.document.documentElement['data-theme'], 'light');
assert.equal(storage.get('quota-dashboard-theme-v2'), 'light');
assert.equal(vm.runInContext('currentLanguage', context), 'ko');
vm.runInContext("setLanguage('en')", context);
assert.equal(vm.runInContext('currentLanguage', context), 'en');
assert.equal(storage.get('quota-dashboard-language'), 'en');
assert.equal(vm.runInContext("t('title')", context), 'EU Steel Safeguard Quota Monitor');
assert.equal(vm.runInContext("displayItemName('열연강판')", context), 'Hot-rolled Flat Products');
vm.runInContext("setLanguage('ko')", context);
assert.match(vm.runInContext("formatBerlinDateTime('2026-09-10T08:24:59Z')", context), /2026-09-10 10:24:59 CEST$/);
assert.match(vm.runInContext("formatBerlinDateTime('2026-01-10T08:24:59Z')", context), /2026-01-10 09:24:59 CET$/);
assert(html.includes('grid-template-columns: repeat(5, minmax(0, 1fr))'));
assert(html.includes('#quota-table th:nth-child(3)'));

vm.runInContext(`
SNAPSHOTS = {
  '2026-09-09': {snapshot_date:'2026-09-09', records:[
    {order_number:'099802', item_name:'열연강판', origin:'Japan', is_exhausted:false, remaining_rate:.10, consumption_rate:.90, pending_t:0},
    {order_number:'099500', origin:'FTA Quota – CSQ', is_exhausted:false, remaining_rate:.20, consumption_rate:.80, pending_t:5}
  ]},
  '2026-09-10': {snapshot_date:'2026-09-10', count:2, total_attempted:2, records:[
    {order_number:'099802', item_name:'열연강판', origin:'Japan', is_exhausted:true, remaining_rate:-.01, consumption_rate:1.01, pending_t:150},
    {order_number:'099500', origin:'FTA Quota – CSQ', is_exhausted:false, remaining_rate:.15, consumption_rate:.85, pending_t:5}
  ], fta_origins:{'099500':{origins:['United Kingdom']}}}
};
DATA = SNAPSHOTS['2026-09-10'];
document.getElementById('history-date').value = '2026-09-10';
`, context);

const summary = vm.runInContext('calculateDailySummary()', context);
assert.deepEqual([...summary.rateJump], ['099802']);
assert.deepEqual([...summary.pendingJump], ['099802']);
assert.deepEqual([...summary.lowRemaining], ['099802']);
vm.runInContext("currentLanguage='en'; renderCollectionHealth(); renderDailySummary()", context);
assert.equal(element('health-title').textContent, 'Collection successful · 2/2');
assert.equal(element('rate-jump-count').textContent, '1');
assert.equal(element('pending-jump-count').textContent, '1');

vm.runInContext("searchTerm='일본'; DAILY_SUMMARY=calculateDailySummary()", context);
assert.equal(vm.runInContext('filteredRecords().length', context), 1);
assert.equal(vm.runInContext("filteredRecords()[0].order_number", context), '099802');
vm.runInContext("searchTerm='영국'", context);
assert.equal(vm.runInContext("filteredRecords()[0].order_number", context), '099500');

vm.runInContext("currentLanguage='ko'; toggleKgColumns()", context);
assert.equal(context.document.body.classList.contains('show-kg'), true);
assert.equal(element('kg-toggle').textContent, 'kg 숨기기');
vm.runInContext("currentLanguage='en'; toggleKgColumns(); toggleKgColumns()", context);
assert.equal(element('kg-toggle').textContent, 'Hide kg');
assert.equal(vm.runInContext("displayOrigin('Other contries')", context), 'Other countries');

vm.runInContext('orderHistoryIndex=null', context);
assert.equal(vm.runInContext("orderHistoryFor('099802').length", context), 2);

// Forecast = Excel FORECAST.LINEAR (OLS); fewer than 5 points -> no forecast.
const fit = vm.runInContext("linearForecast([0,1,2,3,4,5].map(x => ({x, y: 10 + 2 * x})))", context);
assert(Math.abs(fit.slope - 2) < 1e-9 && Math.abs(fit.intercept - 10) < 1e-9);
assert.equal(fit.r2, 1, 'perfect linear trend must report R²=1');
assert.equal(vm.runInContext("linearForecast([{x:0,y:1},{x:1,y:2}])", context), null);
assert.deepEqual([...vm.runInContext("normalizeCompareRange('2026-10-05', '2026-10-01')", context)], ['2026-10-01', '2026-10-05']);

// Quarter rollover, carry-over denominators, and category forecast.
vm.runInContext(`
SNAPSHOTS = {'2026-09-30': {quarter_label:'2026 Q3', records:[{order_number:'1', category:'1A', quarterly_kg:1000, actual_remaining_kg:0}]}};
[1,2,3,4,5].forEach(day => { SNAPSHOTS['2026-10-0' + day] = {quarter_label:'2026 Q4', records:[{order_number:'1', category:'1A', quarterly_kg:1000, amount_kg:2000, transferred_kg:1000, actual_remaining_kg: 2000 - 100 * day, pending_kg:0}]}; });
DATA = SNAPSHOTS['2026-10-01'];
document.getElementById('history-date').value = '2026-10-01';
`, context);
assert.equal(vm.runInContext('previousSnapshot()', context), null, 'must not compare Q4 day 1 with Q3');
vm.runInContext("DATA = SNAPSHOTS['2026-10-02']; document.getElementById('history-date').value = '2026-10-02'", context);
assert.equal(vm.runInContext('previousSnapshot().date', context), '2026-10-01');

// categoryForecast reads the (much smaller) quota_category_summary shape via
// compareCache/HISTORY_DATES, not full snapshots — see fetchCompareRange.
vm.runInContext(`
DATA = {quarter_label:'2026 Q4', start_date_used:'2026-10-01', snapshot_date:'2026-10-05'};
HISTORY_DATES = [5,4,3,2,1].map(day => ({date: '2026-10-0' + day, quarter_label:'2026 Q4'}));
compareCache = {};
[1,2,3,4,5].forEach(day => {
  compareCache['2026-10-0' + day] = {'1A': {item_name:'열연강판', amount:2000, actual: 2000 - 100 * day, pending:0, transferred:1000}};
});
`, context);
// 1A: consumption = 1 - actual / amount(2000): 5% per day from 10-01 -> hits 100% on 10-20.
const forecast = vm.runInContext("categoryForecast('1A')", context);
assert.equal(forecast.count, 5);
assert.equal(forecast.exhaustDate, '2026-10-20');
assert.equal(forecast.endValue, 100);
assert.equal(forecast.lastDate, '2026-10-05');
vm.runInContext(`
HISTORY_DATES = [5,4,3,2,1].map(day => ({date: '2026-10-0' + day, quarter_label:'2026 Q4'}));
compareCache = {};
[1,2,3,4,5].forEach(day => {
  compareCache['2026-10-0' + day] = {'1A': {item_name:'열연강판', amount:100000, actual: 40 + day, pending:0, transferred:0}};
});
`, context);
assert.notEqual(vm.runInContext("categoryForecast('1A').exhaustDate", context), 'done', 'positive remaining volume is not exhausted');
vm.runInContext("HISTORY_DATES = [{date:'2026-10-01', quarter_label:'2026 Q4'}]; compareCache = {'2026-10-01': compareCache['2026-10-01']}", context);
assert.equal(vm.runInContext("categoryForecast('1A').endDate", context), undefined, 'one point -> no forecast');

vm.runInContext(`
SNAPSHOTS = {
  '2026-10-01': {snapshot_date:'2026-10-01', quarter_label:'2026 Q4'},
  '2026-10-05': {snapshot_date:'2026-10-05', quarter_label:'2026 Q4'}
};
latestSnapshotDate = '2026-10-05';
DATA = SNAPSHOTS['2026-10-01'];
`, context);
assert.equal(vm.runInContext('latestSnapshotData().snapshot_date', context), '2026-10-05', 'compare view must use latest snapshot');

vm.runInContext(`
SNAPSHOTS = {
  '2026-09-09': {snapshot_date:'2026-09-09', records:[
    {order_number:'099802', item_name:'열연강판', origin:'Japan', is_exhausted:false, remaining_rate:.10, consumption_rate:.90, pending_t:0},
    {order_number:'099500', origin:'FTA Quota – CSQ', is_exhausted:false, remaining_rate:.20, consumption_rate:.80, pending_t:5}
  ]},
  '2026-09-10': {snapshot_date:'2026-09-10', count:2, total_attempted:2, records:[
    {order_number:'099802', item_name:'열연강판', origin:'Japan', is_exhausted:true, remaining_rate:-.01, consumption_rate:1.01, pending_t:150},
    {order_number:'099500', origin:'FTA Quota – CSQ', is_exhausted:false, remaining_rate:.15, consumption_rate:.85, pending_t:5}
  ], fta_origins:{'099500':{origins:['United Kingdom']}}}
};
DATA = SNAPSHOTS['2026-09-10'];
document.getElementById('history-date').value = '2026-09-10';
`, context);

(async () => {
  let rangeAttempts = 0;
  context.firebase = { firestore: () => ({ collection: () => ({
    where() { return this; },
    async get() {
      rangeAttempts++;
      if (rangeAttempts === 1) throw new Error('temporary');
      return { forEach() {} };
    },
  }) }) };
  vm.runInContext("compareQuarterLoaded=false; compareQuarterPromise=null; compareCache={}; HISTORY_DATES=[{date:'2026-10-01', quarter_label:'2026 Q4'}]", context);
  await assert.rejects(() => vm.runInContext("ensureCompareQuarterLoaded('2026-10-01', '2026-10-05')", context));
  await vm.runInContext("ensureCompareQuarterLoaded('2026-10-01', '2026-10-05')", context);
  assert.equal(rangeAttempts, 2, 'failed compare load must retry');

  let queriedWithoutProjection = false;
  context.firebase = { firestore: () => ({ collection: () => ({
    orderBy() { return this; }, limit() { return this; },
    async get() { queriedWithoutProjection = true; return { docs: [], forEach() {} }; },
  }) }) };
  await vm.runInContext('loadSnapshotQueries()', context);
  assert.equal(queriedWithoutProjection, true, 'browser query must not require server-only select()');

  vm.runInContext("searchTerm=''; activeQuickFilter=null", context);
  await vm.runInContext("exportToExcel('all')", context);
  assert(exportedBlob, 'Excel export should create a Blob');
  assert.equal(vm.runInContext("recordToExportRow(DATA.records[0])[1]", context), 'Hot-rolled Flat Products');
  assert.equal(vm.runInContext("exportHeaders()[4]", context), 'Quarterly quota (kg)');
  assert.equal(vm.runInContext("exportHeaders()[6]", context), 'Carried over (kg)');
  assert.equal(vm.runInContext("exportHeaders()[15]", context), 'Consumption rate');
  assert(source.includes("if (r.is_exhausted)"), 'Excel exhausted-row styling must use scraper truth');

  // Round-trip the bundled Excel library itself to ensure cell fill/font styles
  // survive file generation in this release.
  const styleBook = new ExcelJS.Workbook();
  const styleSheet = styleBook.addWorksheet('QUOTA');
  const styleRow = styleSheet.addRow(['099802']);
  styleRow.getCell(1).fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FFFFC7CE' } };
  styleRow.getCell(1).font = { color: { argb: 'FF9C0006' } };
  const roundTrip = new ExcelJS.Workbook();
  await roundTrip.xlsx.load(await styleBook.xlsx.writeBuffer());
  assert.equal(roundTrip.getWorksheet('QUOTA').getCell('A1').fill.fgColor.argb, 'FFFFC7CE');

  // Compare table lists every category, even ones not charted (max 8 series
  // is a chart/colour limit only — see renderCompare's colorOf fallback).
  vm.runInContext(`
  HISTORY_DATES = [
    {date:'2026-10-02', quarter_label:'2026 Q4'},
    {date:'2026-10-01', quarter_label:'2026 Q4'},
  ];
  compareCache = {
    '2026-10-01': {'1A':{item_name:'열연강판', amount:2000, actual:1900, pending:0, transferred:1000}, '2':{item_name:'냉연', amount:1000, actual:900, pending:0, transferred:0}},
    '2026-10-02': {'1A':{item_name:'열연강판', amount:2000, actual:1800, pending:0, transferred:1000}, '2':{item_name:'냉연', amount:1000, actual:850, pending:0, transferred:0}},
  };
  compareSlots = {'1A': 1};
  latestSnapshotDate = '2026-10-02';
  DATA = {quarter_label:'2026 Q4', start_date_used:'2026-10-01', snapshot_date:'2026-10-02'};
  document.getElementById('cmp-from').value = '2026-10-01';
  document.getElementById('cmp-to').value = '2026-10-02';
  compareQuarterLoaded = true;
  compareRangeKey = '2026-10-01|2026-10-02';
  `, context);
  await vm.runInContext('renderCompare()', context);
  const compareTableHtml = element('cmp-table-body').innerHTML;
  assert.equal((compareTableHtml.match(/<tr>/g) || []).length, 2, 'compare table must list every category, not just the charted ones');
  assert(compareTableHtml.includes('CAT 2'), 'unselected category must still appear in the compare table');
  vm.runInContext("currentLanguage='ko'; compareMeta={'2026-10-02':{success:99,total:100,failed:['099801']}}", context);
  await vm.runInContext('renderCompare()', context);
  assert(element('cmp-table-body').innerHTML.includes('불완전 데이터'), 'partial collection must suppress forecasts');
  vm.runInContext("currentLanguage='en'; compareMeta={}", context);

  // Compare/forecast Excel export mirrors the status export's process:
  // same ExcelJS styling, bold/frozen header, and a red fill on rows whose
  // forecast says the category is already exhausted.
  vm.runInContext(`
  HISTORY_DATES = [5,4,3,2,1].map(day => ({date: '2026-10-0' + day, quarter_label:'2026 Q4'}));
  compareCache = {};
  [1,2,3,4,5].forEach(day => {
    compareCache['2026-10-0' + day] = {
      '1A': {item_name:'열연강판', amount:2000, actual: Math.max(0, 2000 - 500 * day), pending:0, transferred:1000},
      '2': {item_name:'냉연', amount:1000, actual: 1000 - 10 * day, pending:0, transferred:0},
    };
  });
  compareSlots = {'1A': 1, '2': 2};
  latestSnapshotDate = '2026-10-05';
  DATA = {quarter_label:'2026 Q4', start_date_used:'2026-10-01', snapshot_date:'2026-10-05'};
  document.getElementById('cmp-from').value = '2026-10-01';
  document.getElementById('cmp-to').value = '2026-10-05';
  compareQuarterLoaded = true;
  compareRangeKey = '2026-10-01|2026-10-05';
  `, context);
  await vm.runInContext('renderCompare()', context); // populates lastCompareExport
  assert.equal(vm.runInContext("compareExportFilename()", context), 'EU_Steel_Quota_Compare_2026-10-01_2026-10-05_Consumption_rate.xlsx');
  // Row/forecast values themselves (which columns get 0.0% vs #,##0, the
  // red-fill-on-exhausted rule, the quarter-end date in the header) are
  // verified via a real (single-realm) browser round trip — see
  // scratchpad notes; a Node vm context can't reliably round-trip ExcelJS
  // output built from vm-realm arrays, so this test stays at the same
  // depth as the status export's Excel test above.
  exportedBlob = null;
  await vm.runInContext('exportCompareToExcel()', context);
  assert(exportedBlob, 'Compare Excel export should create a Blob');

  console.log('dashboard logic and Excel tests: OK');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
