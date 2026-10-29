import { chromium } from 'playwright';
import { zipSync, strToU8 } from 'fflate';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';

const base = process.env.DEMO_BASE || 'http://127.0.0.1:4173';
const browser = await chromium.launch({headless:true,args:['--no-sandbox']});
const checks = [];
const ok = name => {checks.push(name);console.log('PASS '+name);};

try {
  const context = await browser.newContext({acceptDownloads:true});
  const page = await context.newPage();
  const errors = [];
  const requests = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('request', req => requests.push({method:req.method(),url:req.url()}));
  await page.goto(base+'/');
  assert.match(await page.title(), /rnbatch/);
  assert.equal(await page.locator('input[type=file]').count(),2);
  ok('Cloudflare Pages 静态页面无需 Python 服务可打开');

  const txt = Buffer.from('会议主题：星海中心项目\n项目名称：星海中心\n测试原文内容不会离开本机','utf8');
  await page.locator('#files').setInputFiles([
    {name:'会议纪要.txt',mimeType:'text/plain',buffer:txt},
    {name:'IMG_001.jpg',mimeType:'image/jpeg',buffer:Buffer.from([255,216,255,217])}
  ]);
  await page.getByText('2 个文件',{exact:true}).waitFor();
  assert.equal(await page.locator('#extract').isEnabled(),true);
  ok('浏览器本地批量选择文件并呈现数量与大小');

  await page.locator('#extract').click();
  await page.getByText('已从 1 份文档读取正文',{exact:false}).waitFor();
  assert.ok(await page.locator('.suggestion').count() >= 1);
  ok('从 TXT 文件正文提取关键词建议');
  await page.locator('.suggestion').first().click();
  assert.ok((await page.locator('#keyword').inputValue()).length >=2);
  ok('点击候选关键词填充规则');

  await page.locator('#keyword').fill('黑盒测试');
  await page.locator('#template').selectOption('{kw}-{n:02}');
  await page.locator('#preview').click();
  assert.equal(await page.locator('#preview-rows tr').count(),2);
  assert.match(await page.locator('#preview-rows').innerText(), /黑盒测试-01|黑盒测试-02/);
  assert.equal(await page.locator('#download').isEnabled(),true);
  ok('真实 UI 预览两行改名并检查可下载状态');

  const downloadPromise=page.waitForEvent('download');
  await page.locator('#download').click();
  const download=await downloadPromise;
  assert.equal(download.suggestedFilename(),'rnbatch-整理结果.zip');
  const path=await download.path();
  const output=execFileSync('python',['-c',
    'import json,sys,zipfile; z=zipfile.ZipFile(sys.argv[1]); print(json.dumps({"names":z.namelist(),"text":next(z.read(n).decode("utf-8") for n in z.namelist() if n.endswith(".txt"))},ensure_ascii=False))',
    path],{encoding:'utf8'});
  const unpack=JSON.parse(output);
  assert.equal(unpack.names.length,2);
  assert.ok(unpack.names.some(x=>/^黑盒测试-\d+\.txt$/.test(x)));
  assert.ok(unpack.names.some(x=>/^黑盒测试-\d+\.jpg$/.test(x)));
  assert.equal(unpack.text,txt.toString('utf8'));
  ok('浏览器打包并下载有效的 ZIP，源文件内容未修改');

  await page.locator('#reset').click();
  assert.equal(await page.locator('#file-count').textContent(),'0 个文件');
  assert.equal(await page.locator('#download').isEnabled(),false);
  ok('清空本地文件状态并重置预览');

  await page.locator('#files').setInputFiles([
    {name:'a?.txt',mimeType:'text/plain',buffer:Buffer.from('a')},
    {name:'a*.txt',mimeType:'text/plain',buffer:Buffer.from('b')}
  ]);
  await page.getByText('2 个文件',{exact:true}).waitFor();
  await page.locator('#keyword').fill('演示');
  await page.locator('#template').selectOption('{kw}-{orig}');
  await page.locator('#preview').click();
  assert.equal(await page.locator('#download').isEnabled(),false);
  assert.match(await page.locator('#preview-info').textContent(),/1 个命名冲突/);
  ok('相同目标名冲突被识别并禁止生成 ZIP');

  // Office XML is read entirely in the browser without a backend.
  await page.locator('#reset').click();
  const docx=zipSync({
    '[Content_Types].xml': strToU8('<?xml version="1.0"?><Types/>'),
    'word/document.xml':strToU8('<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>云端演示项目</w:t></w:r></w:p></w:body></w:document>')
  },{level:0});
  await page.locator('#files').setInputFiles([
    {name:'说明.docx',mimeType:'application/vnd.openxmlformats-officedocument.wordprocessingml.document',buffer:Buffer.from(docx)}
  ]);
  await page.locator('#extract').click();
  await page.getByText('已从 1 份文档读取正文',{exact:false}).waitFor();
  assert.ok((await page.locator('#suggestions').innerText()).includes('云端演示项目'));
  ok('Office DOCX 正文可在本地解压提取关键词');

  assert.equal(errors.length,0,errors.join('; '));
  ok('所有页面操作没有未捕获 JavaScript 异常');
  assert.equal(requests.filter(req=>req.method!=='GET').length,0);
  assert.ok(requests.every(req=>req.url.startsWith(base)));
  ok('没有 POST 上传请求与外部网络请求');

  console.log('BROWSER DEMO PASS '+checks.length+'/'+checks.length);
  await context.close();
} catch(e) {
  console.error('BROWSER DEMO FAILED',e);
  process.exitCode=1;
} finally {
  await browser.close();
}
