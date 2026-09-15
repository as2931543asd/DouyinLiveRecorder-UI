const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../webui/static/index.html'), 'utf8');
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)]
  .map(match => match[1]).find(source => source.includes('createApp({'));
const prefix = 'https://live.douyin.com/';

function loadPage(postResult = { ok: true }) {
  let page;
  const requests = [];
  const context = {
    Vue: {
      ref: value => ({ value }),
      reactive: value => value,
      computed: getter => ({ get value() { return getter(); } }),
      nextTick: async () => {},
      onMounted: () => {},
      onUnmounted: () => {},
      createApp: options => ({ mount: () => { page = options.setup(); } }),
    },
    document: { documentElement: { setAttribute: () => {} } },
    localStorage: { getItem: () => null, setItem: () => {} },
    setTimeout: () => 1,
    clearTimeout: () => {},
    fetch: async (url, options = {}) => {
      requests.push({ url, ...options });
      return { json: async () => options.method === 'POST' || options.method === 'PATCH' ? postResult : [] };
    },
  };
  vm.runInNewContext(script, context, { filename: 'index.html:inline-script' });
  return { page, requests };
}

test('quality controls and payload bindings are removed', () => {
  assert.ok(script);
  assert.doesNotMatch(html, /newStreamer\.quality|s\.quality|<label>画质<\/label>/);
  assert.match(html, /@paste="pasteStreamerUrl"/);
  assert.match(html, /:disabled="!canAddStreamer \|\| adding"/);
  const { page } = loadPage();
  assert.equal(page.newStreamer.url, prefix);
  assert.equal(page.canAddStreamer.value, false);
  assert.equal(Object.hasOwn(page.newStreamer, 'quality'), false);
});

test('default prefix cannot be submitted; empty reopen restores prefix and cursor', async () => {
  const { page, requests } = loadPage();
  await page.addStreamer();
  assert.equal(requests.length, 0);
  page.newStreamer.url = '';
  let focused = false;
  let selection;
  page.urlInput.value = {
    get value() { return page.newStreamer.url; },
    focus: () => { focused = true; },
    setSelectionRange: (start, end) => { selection = [start, end]; },
  };
  await page.openAdd();
  assert.equal(page.newStreamer.url, prefix);
  assert.equal(focused, true);
  assert.deepEqual(selection, [prefix.length, prefix.length]);
  page.newStreamer.url += '123456';
  assert.equal(page.canAddStreamer.value, true);
});

test('pasting a complete link replaces the prefix; bare ID keeps normal paste behavior', () => {
  const { page } = loadPage();
  let prevented = false;
  page.pasteStreamerUrl({
    clipboardData: { getData: () => '123456' },
    preventDefault: () => { prevented = true; },
  });
  assert.equal(prevented, false);
  const url = 'https://www.xiaohongshu.com/livestream/123';
  page.pasteStreamerUrl({
    clipboardData: { getData: () => ` ${url} ` },
    preventDefault: () => { prevented = true; },
  });
  assert.equal(prevented, true);
  assert.equal(page.newStreamer.url, url);
});

test('successful add sends no quality and resets the prefix', async () => {
  const { page, requests } = loadPage();
  page.newStreamer.url = prefix + '123456';
  page.newStreamer.name = ' 测试 ';
  await page.addStreamer();
  const posted = requests.find(request => request.method === 'POST');
  assert.deepEqual(JSON.parse(posted.body), { url: prefix + '123456', name: '测试' });
  assert.equal(page.newStreamer.url, prefix);
  assert.equal(page.newStreamer.name, '');
  assert.equal(page.canAddStreamer.value, false);
});

test('new notification controls stay hidden against the not-yet-restarted backend', () => {
  const { page } = loadPage();
  page.streamers.value = [{ url: prefix + '1', state: '录制中' }];
  assert.equal(page.tabs.value.some(tab => tab.key === 'special'), false);
  assert.match(html, /v-if="typeof s\.special_monitor === 'boolean'"/);
  page.streamers.value[0].special_monitor = false;
  assert.equal(page.tabs.value.some(tab => tab.key === 'special'), true);
});

test('special monitor filters independently of live or paused state', () => {
  const { page } = loadPage();
  page.streamers.value = [
    { url: prefix + '1', special_monitor: true, state: '已暂停' },
    { url: prefix + '2', special_monitor: false, state: '录制中' },
  ];
  page.tab.value = 'special';
  assert.equal(page.filteredStreamers.value.length, 1);
  assert.equal(page.countByTab('special'), 1);
  assert.equal(page.filteredStreamers.value[0].url, prefix + '1');
});

test('special monitor uses a channel-neutral explicit setter and blocks double clicks', async () => {
  const { page, requests } = loadPage();
  const streamer = { url: prefix + '1', special_monitor: false };
  const pending = page.toggleSpecialMonitor(streamer);
  await page.toggleSpecialMonitor(streamer);
  await pending;
  const patches = requests.filter(request => request.method === 'PATCH');
  assert.equal(patches.length, 1);
  assert.equal(patches[0].url, '/api/streamers/special-monitor');
  assert.deepEqual(JSON.parse(patches[0].body), { url: streamer.url, enabled: true });
  assert.equal(page.specialBusy.value.length, 0);
  assert.equal(page.toast.type, 'success');
});

test('special monitor failures are visible and never falsely flip the switch', async () => {
  const { page } = loadPage({ ok: false, error: '保存失败' });
  const streamer = { url: prefix + '1', special_monitor: true };
  await page.toggleSpecialMonitor(streamer);
  assert.equal(streamer.special_monitor, true);
  assert.equal(page.toast.msg, '保存失败');
  assert.equal(page.specialBusy.value.length, 0);
});

test('validation errors are visible and do not clear the entered URL', async () => {
  const { page } = loadPage({ detail: [{ msg: '名称不能包含换行' }] });
  page.newStreamer.url = prefix + '123456';
  await page.addStreamer();
  assert.equal(page.toast.msg, '名称不能包含换行');
  assert.equal(page.toast.type, 'error');
  assert.equal(page.newStreamer.url, prefix + '123456');
  assert.equal(page.adding.value, false);
});
