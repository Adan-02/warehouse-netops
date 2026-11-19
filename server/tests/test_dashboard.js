// Execute the actual inline dashboard code against a deterministic browser stub.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function dashboard() {
  const html = fs.readFileSync(path.join(__dirname, '../app/templates/index.html'), 'utf8');
  const source = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/^refresh\(\);$/m, '');
  const nodes = new Map();
  function node(id) {
    if (nodes.has(id)) return nodes.get(id);
    const value = {value:id === 'history-range' ? '10m' : '', disabled:id === 'export-csv',
      listeners:{}, append(){}, replaceChildren(){}, classList:{toggle(){}},
      addEventListener(name,callback){ this.listeners[name] = callback; }};
    nodes.set(id,value);
    return value;
  }
  const clock = {now:Date.parse('2026-11-19T12:00:00Z')};
  class BrowserDate extends Date {
    constructor(...values) { super(...(values.length ? values : [clock.now])); }
    static now() { return clock.now; }
  }
  const context = vm.createContext({
    document:{getElementById:node,createElement:() => node(Symbol())},
    window:{location:{href:''},addEventListener(){}},
    URLSearchParams, Date:BrowserDate, setInterval(){},
    fetch:async () => ({ok:true,json:async () => ({version:'test'})}),
  });
  vm.runInContext(source,context);
  vm.runInContext('drawCharts=()=>{};renderRows=rows=>globalThis.renderedRows=rows;',context);
  const requests = [];
  context.fetch = async url => {
    requests.push(url);
    return {ok:true,json:async () => url.startsWith('/api/history/range')
      ? {resolution_seconds:60,buckets:[{started_at:'2026-11-19T11:59:00Z',temperature_c:{avg:24},humidity_pct:null}]}
      : {readings:[]}};
  };
  return {context,nodes,clock,requests,node,history:() => vm.runInContext('fetchHistory()',context)};
}

test('raw history requests a real ten-minute window and CSV keeps exactly that snapshot', async () => {
  const page = dashboard();
  await page.history();
  const historyUrl = new URL(page.requests[0], 'http://localhost');
  const query = historyUrl.searchParams;
  assert.equal(historyUrl.pathname,'/api/history');
  assert.equal(Date.parse(query.get('end')) - Date.parse(query.get('start')),600000);
  assert.equal(query.get('limit'),'240');
  page.clock.now += 90000;
  page.node('export-csv').listeners.click();
  const exportUrl = new URL(page.context.window.location.href,'http://localhost');
  assert.equal(exportUrl.pathname,'/api/history/export.csv');
  assert.equal(exportUrl.searchParams.get('start'),query.get('start'));
  assert.equal(exportUrl.searchParams.get('end'),query.get('end'));
});

test('aggregation keeps sequence null in the data model', async () => {
  const page = dashboard();
  page.node('history-range').value = '24h';
  await page.history();
  assert.equal(page.context.renderedRows[0].seq,null);
  assert.equal(page.context.renderedRows[0].temperature_c,24);
  assert.equal(page.context.renderedRows[0].humidity_pct,null);
});

test('a late response for a previous selection cannot overwrite the current chart', async () => {
  const page = dashboard();
  let complete;
  page.context.fetch = () => new Promise(resolve => { complete=resolve; });
  const pending = page.history();
  page.node('history-range').value = '7d';
  complete({ok:true,json:async () => ({readings:[{seq:99}]})});
  await pending;
  assert.equal(vm.runInContext('historySelection',page.context),null);
  assert.equal(page.node('export-csv').disabled,true);
  assert.equal(page.context.renderedRows,undefined);
});

test('changing the range invalidates an export even while another refresh is in flight', async () => {
  const page = dashboard();
  await page.history();
  assert.equal(page.node('export-csv').disabled,false);
  vm.runInContext('loading=true;',page.context);
  page.node('history-range').value = '7d';
  page.node('history-range').listeners.change();
  page.node('export-csv').listeners.click();
  assert.equal(page.node('export-csv').disabled,true);
  assert.equal(page.context.window.location.href,'');
});
