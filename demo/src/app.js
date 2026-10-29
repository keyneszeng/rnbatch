import { zipSync, unzipSync } from 'fflate';

const MAX_FILES = 80;
const MAX_TOTAL = 40 * 1024 * 1024;
const MAX_SINGLE = 20 * 1024 * 1024;
const MAX_OFFICE = 5 * 1024 * 1024;
const $ = id => document.getElementById(id);

let items = [];
let plan = null;
let suggestions = [];

function status(message, error = false) {
  const el = $('message');
  el.textContent = message;
  el.classList.toggle('error', error);
}

function safePath(raw) {
  const path = String(raw || '').replace(/\\/g, '/');
  const parts = path.split('/');
  if (!path || parts.some(p => !p || p === '.' || p === '..' || p.startsWith('.') ||
    /[\x00-\x1f\x7f]/.test(p) || p.length > 180)) throw Error('文件路径不安全或过长');
  return parts.join('/');
}

function safeName(raw) {
  return String(raw || '').normalize('NFC')
    .replace(/[\\/<>:"|?*\x00-\x1f\x7f]/g, '-')
    .replace(/^\.+/, '').replace(/\.+$/, '').trim().slice(0, 64);
}
function fileSuffix(name) {
  const m = name.match(/\.([a-zA-Z0-9]{1,12})$/);
  return m ? '.' + m[1] : '';
}
function formatBytes(bytes) {
  return bytes < 1024 ? bytes + ' B' : (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}
function prettyType(file) {
  const ext = fileSuffix(file.name).toLowerCase();
  return ({'.jpg':'照片','.jpeg':'照片','.png':'照片','.webp':'照片',
    '.pdf':'PDF','.doc':'文档','.docx':'文档','.txt':'文本','.md':'文本',
    '.pptx':'演示','.ppt':'演示','.xlsx':'表格','.xls':'表格',
    '.csv':'表格','.json':'数据','.zip':'压缩包','.mp4':'视频'})[ext] || '文件';
}
function pathOf(file) { return safePath(file.webkitRelativePath || file.name); }

function addFileChips() {
  $('file-count').textContent = items.length + ' 个文件';
  const list = $('file-list');
  list.replaceChildren();
  items.slice(0, 12).forEach(item => {
    const span = document.createElement('span');
    span.className = 'file-chip';
    span.textContent = item.path + ' · ' + formatBytes(item.file.size);
    span.title = item.path;
    list.appendChild(span);
  });
  if (items.length > 12) {
    const chip = document.createElement('span');
    chip.className = 'file-chip more';
    chip.textContent = '还有 ' + (items.length - 12) + ' 个文件';
    list.appendChild(chip);
  }
}

function invalidate() {
  plan = null;
  $('download').disabled = true;
  $('preview-rows').replaceChildren();
  const row = document.createElement('tr');
  row.className = 'empty-row';
  const cell = document.createElement('td');
  cell.colSpan = 3;
  cell.textContent = '这里将显示你的批量命名结果';
  row.appendChild(cell);
  $('preview-rows').appendChild(row);
  $('preview-info').textContent = '添加文件并输入关键词后即可预览';
  $('preview').disabled = items.length === 0 || !safeName($('keyword').value);
}

function resetAll() {
  items = [];
  suggestions = [];
  $('keyword').value = '';
  $('suggestions').replaceChildren();
  $('files').value = '';
  $('folder').value = '';
  $('extract').disabled = true;
  $('reset').disabled = true;
  addFileChips();
  invalidate();
  status('已清空全部本地文件列表。');
}

function loadFiles(list) {
  try {
    const files = Array.from(list || []);
    if (!files.length) return;
    if (files.length > MAX_FILES) throw Error('演示版最多处理 ' + MAX_FILES + ' 个文件');
    const total = files.reduce((sum, f) => sum + f.size, 0);
    if (total > MAX_TOTAL || files.some(f => f.size > MAX_SINGLE))
      throw Error('超出文件容量：总量不超过 40 MB，单文件不超过 20 MB');
    const seen = new Set();
    const next = files.map(file => {
      const path = pathOf(file);
      if (seen.has(path.toLocaleLowerCase())) throw Error('存在重复文件路径：' + path);
      seen.add(path.toLocaleLowerCase());
      return { file, path };
    });
    items = next;
    suggestions = [];
    $('suggestions').replaceChildren();
    $('extract').disabled = false;
    $('reset').disabled = false;
    addFileChips();
    invalidate();
    status('已载入 ' + files.length + ' 个文件（' + formatBytes(total) + '），文件没有上传至云端。');
  } catch (e) { status('选择文件失败：' + e.message, true); }
}

$('files').addEventListener('change', e => loadFiles(e.target.files));
$('folder').addEventListener('change', e => loadFiles(e.target.files));
const drop = $('dropzone');
for (const name of ['dragenter', 'dragover']) {
  drop.addEventListener(name, e => { e.preventDefault(); drop.classList.add('dragover'); });
}
for (const name of ['dragleave', 'drop']) {
  drop.addEventListener(name, e => { e.preventDefault(); drop.classList.remove('dragover'); });
}
drop.addEventListener('drop', e => loadFiles(e.dataTransfer?.files));
$('keyword').addEventListener('input', invalidate);
$('template').addEventListener('change', invalidate);
$('sort').addEventListener('change', invalidate);
$('reset').addEventListener('click', resetAll);

const decoder = new TextDecoder('utf-8', { fatal:false });
function xmlText(bytes, tagNames) {
  const xml = new DOMParser().parseFromString(decoder.decode(bytes), 'text/xml');
  if (xml.querySelector('parsererror')) return '';
  const parts = [];
  for (const tag of tagNames) {
    const nodes = Array.from(xml.getElementsByTagName(tag));
    for (const node of nodes) {
      const text = node.textContent?.trim();
      if (text) parts.push(text);
      if (parts.join(' ').length > 12000) return parts.join(' ').slice(0, 12000);
    }
    if (parts.length) break;
  }
  return parts.join(' ').slice(0, 12000);
}
async function readDocument(file) {
  const ext = fileSuffix(file.name).toLowerCase();
  if (['.txt','.md','.csv','.json'].includes(ext))
    return (await file.text()).slice(0, 20000);
  if (!['.docx','.pptx','.xlsx'].includes(ext)) return '';
  if (file.size > MAX_OFFICE) return '';
  const bytes = new Uint8Array(await file.arrayBuffer());
  let zip;
  try {
    zip = unzipSync(bytes, {filter:info => info.originalSize <= 250000 &&
      ((ext === '.docx' && info.name === 'word/document.xml') ||
       (ext === '.pptx' && /^ppt\/slides\/slide\d+\.xml$/.test(info.name)) ||
       (ext === '.xlsx' && (info.name === 'xl/sharedStrings.xml' ||
         /^xl\/worksheets\/sheet\d+\.xml$/.test(info.name))))});
  } catch { return ''; }
  const paths = Object.keys(zip).sort((a,b)=>a.localeCompare(b,undefined,{numeric:true}));
  let result = '';
  for (const path of paths.slice(0, 12)) {
    result += xmlText(zip[path], ext === '.docx' ? ['w:t'] :
      ext === '.pptx' ? ['a:t'] : ['t', 'v']) + '\n';
    if (result.length > 18000) break;
  }
  return result.slice(0, 18000);
}
function findCandidates(text) {
  const found = [];
  const add = (str) => {
    const s = safeName(str.replace(/[，。；：、!！?？\n\r\t]/g,' ').trim());
    if (s.length >= 2 && s.length <= 40 && !found.includes(s)) found.push(s);
  };
  const matches = text.match(/[\p{Script=Han}a-zA-Z0-9]{2,22}(?:项目|中心|工程|协会|会议|活动|计划|论坛)/gu) || [];
  matches.slice(0, 8).forEach(add);
  for (const line of text.split(/[\n\r]/).slice(0, 12)) {
    const split = line.split(/[：:]/);
    if (split.length > 1 && /项目|主题|名称|会议|工程|活动/.test(split[0])) add(split[1].slice(0,32));
  }
  return found.slice(0, 8);
}
$('extract').addEventListener('click', async () => {
  $('extract').disabled = true;
  status('正在浏览器本地读取文档文字…');
  try {
    let text = '';
    let sources = 0;
    for (const item of items.slice(0, 16)) {
      const value = await readDocument(item.file);
      if (value.trim()) { text += '\n' + value; sources++; }
      if (text.length > 30000) break;
    }
    suggestions = findCandidates(text);
    const host = $('suggestions');
    host.replaceChildren();
    for (const candidate of suggestions) {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'suggestion';
      button.textContent = candidate;
      button.addEventListener('click', () => { $('keyword').value = candidate; invalidate(); });
      host.appendChild(button);
    }
    if (suggestions.length && !safeName($('keyword').value)) {
      $('keyword').value = suggestions[0];
      invalidate();
    }
    status(sources ? '已从 ' + sources + ' 份文档读取正文，推荐 ' + suggestions.length + ' 个关键词。' :
      '没有检测到可读取的文字，请手动输入项目关键词。');
  } catch (e) { status('读取文字失败：' + e.message + '。可以手动输入关键词。', true); }
  finally { $('extract').disabled = false; }
});

function makePlan() {
  if (!items.length) throw Error('请先选择文件');
  const kw = safeName($('keyword').value);
  if (!kw) throw Error('请输入有效关键词');
  const sorted = [...items].sort((a,b) => {
    const key = $('sort').value;
    if (key === 'size') return a.file.size - b.file.size || a.path.localeCompare(b.path, 'zh-CN', { numeric:true });
    if (key === 'date') return a.file.lastModified - b.file.lastModified || a.path.localeCompare(b.path,'zh-CN');
    return a.path.localeCompare(b.path,'zh-CN',{numeric:true});
  });
  const date = new Date();
  const datestr = ''+date.getFullYear()+String(date.getMonth()+1).padStart(2,'0')+
    String(date.getDate()).padStart(2,'0');
  const seen = new Set();
  return sorted.map((item,i) => {
    const slash = item.path.lastIndexOf('/');
    const dir = slash >= 0 ? item.path.slice(0,slash+1) : '';
    const suffix = fileSuffix(item.file.name);
    const stem = item.file.name.slice(0, item.file.name.length-suffix.length);
    const original = safeName(stem) || 'file';
    const n = String(i+1).padStart(2,'0');
    const base = $('template').value.replaceAll('{kw}', kw).replaceAll('{n:02}',n)
      .replaceAll('{date}',datestr).replaceAll('{orig}',original).replaceAll('{type}', prettyType(item.file));
    const name = safeName(base) || 'file';
    const output = dir + name + suffix;
    const normalized = output.toLocaleLowerCase();
    const conflict = seen.has(normalized);
    seen.add(normalized);
    return {...item, output, conflict};
  });
}
$('preview').addEventListener('click', () => {
  try {
    plan = makePlan();
    const host = $('preview-rows');
    host.replaceChildren();
    let conflicts = 0;
    for (const item of plan) {
      if (item.conflict) conflicts++;
      const tr = document.createElement('tr');
      for (const text of [item.path, item.output]) {
        const td = document.createElement('td'); td.textContent = text; tr.appendChild(td);
      }
      const state = document.createElement('td');
      state.textContent = item.conflict ? '重复冲突' : item.path === item.output ? '未改变' : '可下载';
      state.className = item.conflict ? 'state-error' : item.path === item.output ? 'state-same' : 'state-ok';
      tr.appendChild(state); host.appendChild(tr);
    }
    $('preview-info').textContent = plan.length + ' 个文件，' + conflicts + ' 个命名冲突';
    $('download').disabled = conflicts > 0;
    status(conflicts ? '发现重复文件名，请更换命名规则。' :
      '预览完成：文件名已确认，可以生成 ZIP 下载。', conflicts > 0);
  } catch(e) { status('预览失败：' + e.message, true); }
});
$('download').addEventListener('click', async () => {
  if (!plan || plan.some(p => p.conflict)) return;
  const button = $('download');
  button.disabled = true;
  status('正在本地生成 ZIP 文件，请不要关闭页面…');
  try {
    const parts = {};
    for (const [index,item] of plan.entries()) {
      parts[item.output] = new Uint8Array(await item.file.arrayBuffer());
      status('正在打包 ' + (index+1) + ' / ' + plan.length + '：' + item.path);
    }
    // fflate creates standard UTF-8 ZIP entries. Level 0 avoids expensive
    // recompression of images/PDFs and keeps the free demo responsive.
    const zip = zipSync(parts, {level:0});
    const blob = new Blob([zip], {type:'application/zip'});
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url; link.download = 'rnbatch-整理结果.zip';
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    status('ZIP 已生成并开始下载（' + formatBytes(blob.size) + '）。原始文件完全没有修改。');
  } catch(e) { status('ZIP 生成失败：' + e.message, true); }
  finally { button.disabled = false; }
});

// There are intentionally no requests to any upload, authentication, or API
// endpoints. Everything above is executed on the visitor's own device.
