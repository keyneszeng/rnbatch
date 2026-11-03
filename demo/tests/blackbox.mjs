import { chromium, webkit } from 'playwright';
import { PDFDocument, StandardFonts } from 'pdf-lib';
import { zipSync, unzipSync, strToU8 } from 'fflate';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';

const base = process.env.DEMO_BASE || 'http://127.0.0.1:4173';
const browserType = process.env.DEMO_BROWSER === 'webkit' ? webkit : chromium;
const browser = await browserType.launch({headless:true,...(browserType === chromium ? {args:['--no-sandbox']} : {})});
const checks = [];
const ok = name => {checks.push(name);console.log('PASS '+name);};

try {
  const context = await browser.newContext({acceptDownloads:true});
  const page = await context.newPage();
  const errors = [];
  const requests = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (['warn','error'].includes(m.type())) console.log('BROWSER CONSOLE '+m.type()+': '+m.text()); });
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

  await page.locator('#reset').click();
  await page.locator('#files').setInputFiles([
    {name:'一号.txt',mimeType:'text/plain',buffer:Buffer.from('one')}
  ]);
  await page.locator('#keyword').fill('追加验证');
  await page.locator('#preview').click();
  assert.equal(await page.locator('#download').isEnabled(),true);
  await page.locator('#files').setInputFiles([
    {name:'二号.txt',mimeType:'text/plain',buffer:Buffer.from('two')}
  ]);
  assert.equal(await page.locator('#file-count').textContent(),'2 个文件');
  assert.equal(await page.locator('#download').isEnabled(),false);
  assert.match(await page.locator('#file-list').innerText(),/一号.txt/);
  assert.match(await page.locator('#file-list').innerText(),/二号.txt/);
  ok('分批选择文件采用追加，不覆盖旧文件；旧预览自动失效');

  await page.locator('#files').setInputFiles([
    {name:'二号.txt',mimeType:'text/plain',buffer:Buffer.from('another')}
  ]);
  assert.equal(await page.locator('#file-count').textContent(),'2 个文件');
  assert.match(await page.locator('#message').innerText(),/重复文件路径/);
  ok('重复文件路径被拒绝，已选择的文件保持不变');

  await page.locator('#template').selectOption('{kw}-{n:02}');
  await page.locator('#keyword').fill('__proto__');
  await page.locator('#preview').click();
  const specialDownload=page.waitForEvent('download');
  await page.locator('#download').click();
  const special=await specialDownload;
  const specialZip=unzipSync(new Uint8Array(await (await import('node:fs/promises')).readFile(await special.path())));
  assert.equal(Object.keys(specialZip).length,2);
  ok('特殊 JavaScript 对象关键字作为文件名前缀不会丢失 ZIP 文件');

  await page.locator('#reset').click();
  await page.locator('#files').setInputFiles(
    Array.from({length:80},(_,i)=>({
      name:'sample-'+i+'.txt',mimeType:'text/plain',buffer:Buffer.from('ok')
    }))
  );
  assert.equal(await page.locator('#file-count').textContent(),'80 个文件');
  await page.locator('#files').setInputFiles([
    {name:'over-limit.txt',mimeType:'text/plain',buffer:Buffer.from('ok')}
  ]);
  assert.equal(await page.locator('#file-count').textContent(),'80 个文件');
  assert.match(await page.locator('#message').innerText(),/最多处理 80 个文件/);
  ok('累计 80 文件数量限制阻断追加且不会清空原有文件');

  const keyboardReachable=await page.locator('#files').evaluate(el=>{
    el.focus();
    return document.activeElement===el && getComputedStyle(el).display!=='none';
  });
  assert.equal(keyboardReachable,true);
  ok('文件选择控件可以获得键盘焦点');

  // True browser PDF extraction: a real document encoded by pdf-lib.
  await page.locator('#reset').click();
  const pdf = await PDFDocument.create();
  const pdfPage = pdf.addPage([612,792]);
  const pdfFont = await pdf.embedFont(StandardFonts.Helvetica);
  pdfPage.drawText('Project: Blue Harbor Renovation', {x:48,y:730,size:16,font:pdfFont});
  const pdfBytes = await pdf.save();
  await page.locator('#files').setInputFiles({
    name:'real-text.pdf',mimeType:'application/pdf',buffer:Buffer.from(pdfBytes)
  });
  console.log('PDF PRE-EXTRACT', 'pdfBytes='+pdfBytes.length,
    'files='+await page.locator('#file-count').innerText(),
    'message='+await page.locator('#message').innerText());
  await page.locator('#extract').click();
  await page.waitForFunction(() => {
    const message = document.getElementById('message')?.textContent || '';
    return message.includes('已从 ') || message.includes('没有检测到可读取的文字') || message.includes('读取文字失败');
  }, null, {timeout:30000});
  const pdfStatus = await page.locator('#message').innerText();
  console.log('PDF EXTRACT STATUS:', pdfStatus);
  assert.match(pdfStatus, /已从 1 份文档读取正文/);
  const pdfSuggestions=await page.locator('#suggestions').innerText();
  assert.match(pdfSuggestions,/Blue Harbor Renovation/);
  ok('文字型 PDF 使用 PDF.js Worker 在本地读取项目名');

  await page.locator('#reset').click();
  const emptyPdf = await PDFDocument.create();
  emptyPdf.addPage([612,792]);
  await page.locator('#files').setInputFiles({
    name:'scanned-with-no-text-layer.pdf',mimeType:'application/pdf',
    buffer:Buffer.from(await emptyPdf.save())
  });
  await page.locator('#extract').click();
  await page.getByText('没有检测到可读取的文字',{exact:false}).waitFor({timeout:30000});
  assert.equal(await page.locator('#suggestions button').count(),0);
  ok('扫描/无文字层 PDF 显示清晰降级提示，不假装 OCR 成功');

  await page.locator('#reset').click();
  const pptx=zipSync({
    '[Content_Types].xml':strToU8('<?xml version="1.0"?><Types/>'),
    'ppt/slides/slide1.xml':strToU8('<?xml version="1.0"?><p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>海湾物流中心项目</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>')
  },{level:0});
  await page.locator('#files').setInputFiles({
    name:'planning.pptx',mimeType:'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    buffer:Buffer.from(pptx)
  });
  await page.locator('#extract').click();
  await page.getByText('已从 1 份文档读取正文',{exact:false}).waitFor();
  assert.match(await page.locator('#suggestions').innerText(),/海湾物流中心项目/);
  ok('PPTX 演示文稿文本可从 OOXML 提取');

  await page.locator('#reset').click();
  const xlsx=zipSync({
    '[Content_Types].xml':strToU8('<?xml version="1.0"?><Types/>'),
    'xl/sharedStrings.xml':strToU8('<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>西湖年度会议</t></si></sst>')
  },{level:0});
  await page.locator('#files').setInputFiles({
    name:'meeting.xlsx',mimeType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    buffer:Buffer.from(xlsx)
  });
  await page.locator('#extract').click();
  await page.getByText('已从 1 份文档读取正文',{exact:false}).waitFor();
  assert.match(await page.locator('#suggestions').innerText(),/西湖年度会议/);
  ok('XLSX 表格共享字符串可从 OOXML 提取');

  await page.locator('#reset').click();
  const largeBytes=Buffer.alloc(12*1024*1024,0x4b);
  await page.locator('#files').setInputFiles({
    name:'12MB-test.bin',mimeType:'application/octet-stream',buffer:largeBytes
  });
  await page.locator('#keyword').fill('大文件');
  await page.locator('#preview').click();
  const largeDownloadP=page.waitForEvent('download',{timeout:30000});
  const start=Date.now();
  await page.locator('#download').click();
  const largeDownload=await largeDownloadP;
  const largeBuf=await (await import('node:fs/promises')).readFile(await largeDownload.path());
  const largeZip=unzipSync(new Uint8Array(largeBuf));
  assert.equal(Object.values(largeZip)[0].length,largeBytes.length);
  ok('12MB 文件下载与内容长度一致，用时 '+(Date.now()-start)+'ms');

  assert.equal(errors.length,0,errors.join('; '));
  ok('所有页面操作没有未捕获 JavaScript 异常');
  assert.equal(requests.filter(req=>req.method!=='GET').length,0);
  assert.ok(requests.every(req=>req.url.startsWith(base)));
  ok('没有 POST 上传请求与外部网络请求');

  console.log('BROWSER DEMO PASS '+checks.length+'/'+checks.length+' ['+(process.env.DEMO_BROWSER||'chromium')+']');
  await context.close();
} catch(e) {
  console.error('BROWSER DEMO FAILED',e);
  process.exitCode=1;
} finally {
  await browser.close();
}
