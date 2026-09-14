'use strict';
const $ = id => document.getElementById(id);
const form = $('search-form');
const player = $('player');
const dialog = $('video-dialog');
let appliedQuery = new URLSearchParams();
let currentPage = 1, totalPages = 1, searchController, locateController, selectedVideo;
let refreshing = false;
const formatter = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  second: '2-digit', fractionalSecondDigits: 3, hourCycle: 'h23',
});
const dateText = ms => ms == null ? '时间未知' : formatter.format(new Date(ms));
function duration(ms) {
  if (ms == null || ms < 0) return '未知';
  const seconds = Math.floor(ms / 1000);
  return [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60]
    .map(v => String(v).padStart(2, '0')).join(':') + '.' + String(Math.floor(ms % 1000)).padStart(3, '0');
}
function message(id, text, error = false) {
  const target = $(id);
  target.textContent = text;
  target.hidden = !text;
  target.classList.toggle('error', error);
}
async function api(path, options = {}) {
  const response = await fetch('/api/danmaku' + path, options);
  let data;
  try { data = await response.json(); }
  catch { throw new Error('接口未就绪。若刚更新程序，请在方便时重启录制服务。'); }
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `请求失败（${response.status}），请检查输入条件`);
  return data;
}
function showStatus(status) {
  $('chat-count').textContent = status.chats.toLocaleString();
  $('file-count').textContent = status.files.toLocaleString();
  $('updated-at').textContent = status.updated_at ? dateText(status.updated_at) : '尚未建立索引';
  $('root').textContent = '录制目录：' + status.root;
  const warnings = [];
  if (status.bad_records) warnings.push(`已跳过 ${status.bad_records} 条损坏记录`);
  if (status.pending_files) warnings.push(`${status.pending_files} 个文件的末尾记录尚未写完`);
  if (status.file_errors) warnings.push(`${status.file_errors} 个文件/目录读取失败`);
  message('index-message', (status.updated_at ? '搜索使用本地索引；新弹幕录入后，请点击“刷新弹幕索引”。' : '首次使用将建立索引，原始弹幕和录像不会被修改。') + (warnings.length ? ' ' + warnings.join('；') : ''));
}
async function refreshIndex() {
  if (refreshing) return;
  refreshing = true;
  $('refresh').disabled = true;
  $('refresh').textContent = '正在刷新…';
  message('index-message', '正在增量读取弹幕文件，首次建立索引可能需要一些时间；不会重新读取未变化的文件。');
  try {
    const status = await api('/index/refresh', {method: 'POST'});
    showStatus(status);
    await search(1);
  } catch (error) { message('index-message', error.message, true); }
  finally {
    refreshing = false;
    $('refresh').disabled = false;
    $('refresh').textContent = '↻ 刷新弹幕索引';
  }
}
function queryFromForm() {
  const query = new URLSearchParams();
  for (const name of ['q', 'scope', 'match', 'anchor', 'viewer', 'order', 'page_size']) {
    query.set(name, form.elements[name].value.trim());
  }
  for (const name of ['since', 'until']) {
    if ($(name).value) {
      const value = new Date($(name).value).getTime();
      if (!Number.isFinite(value) || value < 0) throw new Error('请输入有效的日期时间');
      query.set(name + '_ms', value);
    }
  }
  if (query.has('since_ms') && query.has('until_ms') && +query.get('since_ms') > +query.get('until_ms')) {
    throw new Error('开始时间不能晚于结束时间');
  }
  return query;
}
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text != null) element.textContent = text;
  return element;
}
function highlight(element, text, keywords) {
  // Never interpolate recorded chat/nicknames into HTML.
  if (!keywords.length) { element.textContent = text; return; }
  const escaped = keywords.map(word => word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const parts = text.split(new RegExp('(' + escaped.join('|') + ')', 'gi'));
  parts.forEach((part, i) => element.append(i % 2 ? node('mark', '', part) : document.createTextNode(part)));
}
function renderResults(data, query) {
  $('results').replaceChildren();
  const keywords = (query.get('q') || '').split(/\s+/).filter(Boolean);
  const fields = {content: '弹幕', anchor: '主播', viewer: '发言者'};
  for (const item of data.results) {
    const article = node('article', 'result');
    const top = node('div', 'result-top');
    const identity = node('div', 'result-identity');
    const anchor = node('strong'); highlight(anchor, item.anchor, keywords);
    identity.append(anchor, node('span', 'viewer', '· ' + (item.viewer_nickname || '匿名发言者')));
    const time = node('time', '', dateText(item.timestamp_ms));
    if (item.timestamp_ms != null) time.dateTime = new Date(item.timestamp_ms).toISOString();
    top.append(identity, time);
    const content = node('p', 'content'); highlight(content, item.content, keywords);
    const bottom = node('div', 'result-bottom');
    const source = node('div', 'source');
    source.append(node('p', '', `录制内 ${duration(item.offset_ms)} · 用户 ID：${item.viewer_id || '未知'}${item.matched_fields.length ? ' · 命中 ' + item.matched_fields.map(f => fields[f]).join(' / ') : ''}`));
    source.append(node('p', 'path', `${item.file} : ${item.line}`));
    const button = node('button', 'secondary', '定位视频 ↗');
    button.type = 'button';
    button.addEventListener('click', () => openVideo(item));
    bottom.append(source, button);
    article.append(top, content, bottom);
    $('results').append(article);
  }
  $('total').textContent = data.total.toLocaleString();
  currentPage = data.page;
  totalPages = Math.max(1, Math.ceil(data.total / data.page_size));
  $('page-info').textContent = `第 ${currentPage} / ${totalPages} 页`;
  $('result-description').textContent = `${query.get('order') === 'asc' ? '最早' : '最新'}在前 · 共 ${data.total.toLocaleString()} 条，点击结果定位录像`;
  message('search-message', data.results.length ? '' : '没有找到弹幕。试试缩短关键词、扩大时间范围，或刷新索引。');
}
async function search(page) {
  if (searchController) searchController.abort();
  const controller = new AbortController(); searchController = controller;
  const query = new URLSearchParams(appliedQuery); query.set('page', page);
  $('search-button').disabled = true;
  $('prev').disabled = $('next').disabled = true;
  $('results').setAttribute('aria-busy', 'true');
  message('search-message', '正在搜索…');
  try {
    const data = await api('/search?' + query, {signal: controller.signal});
    if (searchController === controller) renderResults(data, query);
  } catch (error) {
    if (error.name !== 'AbortError') {
      $('results').replaceChildren();
      $('total').textContent = '—';
      currentPage = totalPages = 1;
      $('page-info').textContent = '搜索未完成';
      message('search-message', error.message, true);
    }
  } finally {
    if (searchController === controller) {
      $('search-button').disabled = false;
      $('results').setAttribute('aria-busy', 'false');
      $('prev').disabled = currentPage <= 1;
      $('next').disabled = currentPage >= totalPages;
    }
  }
}
const mediaUrl = (file, download = false) => '/api/danmaku/media?' + new URLSearchParams({file, download});
function stopPlayer() { player.pause(); player.removeAttribute('src'); player.load(); }
async function openVideo(item) {
  if (locateController) locateController.abort();
  const controller = new AbortController(); locateController = controller;
  stopPlayer(); selectedVideo = null;
  $('video-detail').hidden = true;
  $('candidates').replaceChildren();
  message('locate-message', '正在读取分段时间表或探测视频时长…');
  message('player-message', '');
  $('copy-path').textContent = '复制完整路径';
  dialog.showModal();
  try {
    const data = await api('/locate?' + new URLSearchParams({file: item.file, line: item.line}), {signal: controller.signal});
    if (locateController !== controller || !dialog.open) return;
    message('locate-message', data.reason, data.status === 'unresolved');
    if (data.status === 'unresolved') {
      $('candidates').append(node('li', '', data.timing_note));
      for (const file of data.candidates || []) {
        const li = node('li'); const link = node('a', '', file);
        link.href = mediaUrl(file, true); li.append(link); $('candidates').append(li);
      }
      return;
    }
    selectedVideo = data;
    $('video-detail').hidden = false;
    $('selected-chat').textContent = `${item.anchor} · ${dateText(item.timestamp_ms)}\n${item.viewer_nickname || '匿名发言者'}：${item.content}`;
    $('location-badge').textContent = data.status === 'located' ? '实际分段时间表' : '历史时长估算';
    $('video-offset').textContent = '片内 ' + duration(data.offset_ms);
    $('video-path').textContent = data.absolute_path;
    $('timing-note').textContent = data.timing_note;
    $('download').href = mediaUrl(data.file, true);
    player.src = mediaUrl(data.file);
    for (const file of data.alternatives || []) {
      const li = node('li'); const link = node('a', '', '其他格式：' + file);
      link.href = mediaUrl(file, true); li.append(link); $('candidates').append(li);
    }
  } catch (error) { if (error.name !== 'AbortError') message('locate-message', error.message, true); }
}
function seek(secondsBefore = 0) {
  if (!selectedVideo || player.readyState < 1) {
    message('player-message', '视频尚未就绪，或浏览器不支持此格式。可下载后按片内时间定位。', true); return;
  }
  const seconds = Math.max(0, selectedVideo.offset_ms / 1000 - secondsBefore);
  if (Number.isFinite(player.duration) && seconds >= player.duration) {
    message('player-message', '定位时间超出播放器报告的时长，请核对视频是否仍在转码或时间轴有偏差。', true); return;
  }
  try { player.currentTime = seconds; message('player-message', `已跳转至 ${duration(seconds * 1000)}，点击播放器开始播放。`); }
  catch { message('player-message', '此视频暂不支持跳转，请下载后用本地播放器打开。', true); }
}
player.addEventListener('loadedmetadata', () => seek());
player.addEventListener('error', () => {
  if (selectedVideo) message('player-message', '浏览器无法播放此视频编码/格式，请下载并使用本地播放器。', true);
});
$('seek').onclick = () => seek();
$('preroll').onclick = () => seek(5);
$('close-dialog').onclick = () => dialog.close();
dialog.addEventListener('close', () => { if (locateController) locateController.abort(); selectedVideo = null; stopPlayer(); });
$('copy-path').onclick = async () => {
  if (!selectedVideo) return;
  try { await navigator.clipboard.writeText(selectedVideo.absolute_path); $('copy-path').textContent = '已复制'; }
  catch {
    const range = document.createRange(); range.selectNodeContents($('video-path'));
    const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
    message('player-message', '浏览器不允许自动复制，已选中完整路径，请手动复制。');
  }
};
form.addEventListener('submit', event => {
  event.preventDefault();
  try { appliedQuery = queryFromForm(); search(1); }
  catch (error) { message('search-message', error.message, true); }
});
form.addEventListener('reset', () => { setTimeout(() => { appliedQuery = queryFromForm(); search(1); }, 0); });
$('prev').onclick = () => search(currentPage - 1);
$('next').onclick = () => search(currentPage + 1);
$('refresh').onclick = refreshIndex;
$('timezone').textContent = '按浏览器本地时区输入与显示：' + Intl.DateTimeFormat().resolvedOptions().timeZone;
(async () => {
  appliedQuery = queryFromForm();
  try {
    const status = await api('/index'); showStatus(status);
    if (!status.updated_at && !status.refreshing) await refreshIndex();
    else await search(1);
  } catch (error) { message('index-message', error.message, true); message('search-message', '无法连接检索服务，请稍后刷新页面。', true); }
})();
