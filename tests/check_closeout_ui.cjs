/* Run with: node tests/check_closeout_ui.cjs
 * Exercises the actual inline request/refresh functions without a browser or
 * any broker calls. In particular, the initial journal response is held open
 * until the status endpoint has reported a newer completed closeout.
 */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const turn = () => new Promise(resolve => setImmediate(resolve));

async function check(page) {
  const file = path.join(__dirname, '..', 'src', 'underwriter', 'static', page + '.html');
  const source = fs.readFileSync(file, 'utf8');
  const loadStart = source.indexOf('async function load(background=false)');
  const loadEnd = source.indexOf('\nfunction render(){', loadStart);
  const helperStart = source.indexOf('let closeoutVersion=');
  const helperEnd = source.indexOf("document.addEventListener('visibilitychange'", helperStart);
  assert.ok(loadStart >= 0 && loadEnd > loadStart && helperStart >= 0 && helperEnd > helperStart);
  let helpers = source.slice(helperStart, helperEnd);
  const editStart = helpers.indexOf('function closeoutEditing()');
  const editEnd = helpers.indexOf('function renderCloseout(', editStart);
  helpers = helpers.slice(0, editStart) + 'function closeoutEditing(){return editing}\n' + helpers.slice(editEnd);

  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      textContent: '', dataset: {}, title: '', hidden: false, disabled: false,
      className: '', classList: { toggle() {}, remove() {} }
    });
    return elements.get(id);
  };
  let resolveInitial, summaryCalls = 0, failNext = false;
  const initial = new Promise(resolve => { resolveInitial = resolve; });
  const context = {
    AbortController, URLSearchParams, setTimeout, clearTimeout,
    document: { visibilityState: 'visible' },
    window: { scrollX: 0, scrollY: 450, scrollTo() {} },
    $: element, stamp: value => value,
    __meta: {
      enabled: true, status: 'complete', session_date: '2026-10-19',
      completed_session_date: '2026-10-19', completed_at: '2026-10-19T20:20:05Z',
      observed_at: '2026-10-19T20:20:00Z'
    },
    __summary: async () => {
      summaryCalls++;
      if (summaryCalls === 1) return initial;
      if (failNext) { failNext = false; throw Error('Temporary journal read failure'); }
      return { snapshot: 'after-closeout' };
    }
  };
  vm.createContext(context);
  vm.runInContext(`
    let data=null,summaryBusy=false,archiveBusy=false,summaryRequest=0,request=0;
    let summaryController=null,archiveController=null,controller=null,initialScroll=false;
    let editing=false,renders=[],archiveModes=[];const selected={};
    async function get(url){return url.startsWith('/api/history')?await __summary():__meta}
    async function fetch(url){return {ok:true,json:async()=>url.startsWith('/api/reports')?await __summary():__meta}}
    function render(){renders.push(data.snapshot)}
    function resetPages(){}
    async function loadArchive(preserve=false){archiveModes.push(preserve)}
    ${source.slice(loadStart, loadEnd)}
    ${helpers}
  `, context);

  // The journal read began before the first completed marker was known.
  const initialLoad = vm.runInContext('load()', context);
  await turn();
  await vm.runInContext('checkCloseout()', context);
  assert.equal(summaryCalls, 1, page + ': do not compete with the in-flight initial read');
  assert.equal(vm.runInContext('pendingCloseoutRefresh', context), true);
  resolveInitial({ snapshot: 'before-closeout' });
  await initialLoad;
  await turn();
  await turn();
  assert.equal(summaryCalls, 2, page + ': require one post-status aggregate read');
  assert.equal(vm.runInContext('renders.at(-1)', context), 'after-closeout');
  assert.equal(vm.runInContext('verifiedCloseoutVersion', context), context.__meta.completed_at);
  assert.equal(vm.runInContext('pendingCloseoutRefresh', context), false);
  if (page === 'history') assert.equal(vm.runInContext('archiveModes.at(-1)', context), true);

  // The identical marker is now verified and must not re-query aggregates.
  await vm.runInContext('checkCloseout()', context);
  assert.equal(summaryCalls, 2, page + ': an unchanged verified marker remains idle');

  // Failed current sessions keep the prior successful session separate.
  context.__meta = { ...context.__meta, status: 'failed', session_date: '2026-10-20', retry_at: '2026-10-20T20:25:00Z' };
  await vm.runInContext('checkCloseout()', context);
  assert.match(element('eod-status').textContent, /failed for 2026-10-20.*Retry/);
  assert.equal(element('eod-status').dataset.state, 'failed');
  assert.equal(summaryCalls, 2);

  // A newer completion waits for edits and refreshes after they finish.
  vm.runInContext('editing=true', context);
  context.__meta = { ...context.__meta, status: 'complete', completed_session_date: '2026-10-20', completed_at: '2026-10-20T20:25:05Z' };
  await vm.runInContext('checkCloseout()', context);
  assert.equal(summaryCalls, 2);
  vm.runInContext('editing=false', context);
  await vm.runInContext('applyCloseoutRefresh()', context);
  assert.equal(summaryCalls, 3);

  // A failed aggregate read stays pending without a tight immediate retry loop.
  context.__meta = { ...context.__meta, completed_at: '2026-10-21T20:20:05Z' };
  failNext = true;
  await vm.runInContext('checkCloseout()', context);
  await turn();
  assert.equal(summaryCalls, 4);
  assert.equal(vm.runInContext('pendingCloseoutRefresh', context), true);
  await vm.runInContext('checkCloseout()', context);
  assert.equal(summaryCalls, 5);
  assert.equal(vm.runInContext('pendingCloseoutRefresh', context), false);

  context.document.visibilityState = 'hidden';
  context.__meta = { ...context.__meta, completed_at: '2026-10-22T20:20:05Z' };
  await vm.runInContext('checkCloseout()', context);
  assert.equal(summaryCalls, 5);
  console.log(page + ': initial completion race, verified marker, retry, edit and hidden-tab checks passed');
}

(async () => { for (const page of ['history', 'reports']) await check(page); })().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
