// Live production smoke test: synthetic files only, no credentials or user data.
// Runs in GitHub Actions on demand or after main pushes.
import { chromium, firefox, devices } from 'playwright';
import assert from 'node:assert/strict';
import dns from 'node:dns/promises';
import { writeFileSync, mkdirSync } from 'node:fs';
import { zipSync, strToU8, unzipSync, strFromU8 } from 'fflate';
import { PDFDocument, StandardFonts } from 'pdf-lib';

const ROOT = process.env.LIVE_BASE || 'https://rnbatch.aibuild.work';
const dest = process.env.LIVE_REPORT_DIR || '/tmp/rnbatch-live';
const findings = [];
const browserName = process.env.LIVE_BROWSER || 'chromium';
const browserType = ({chromium, firefox})[browserName];
if (!browserType) throw Error('Unsupported LIVE_BROWSER');
const browser = await browserType.launch({headless:true,...(browserType === chromium ? {args:['--no-sandbox']} : {})});
function log(label, value) { findings.push({label, value}); console.log('CHECK', label, value); }
mkdirSync(dest, {recursive: true});

try {
  // Network and DNS are deliberately checked in the CI runner, not guessed.
  const target = new URL(ROOT);
  const ips = await dns.lookup(target.hostname, {all:true});
  assert.ok(ips.length, '域名没有有效 DNS 记录');
  log('DNS resolved', ips.map(x=>x.address).join(','));

  const ctx = await browser.newContext({acceptDownloads:true});
  const page = await ctx.newPage();
  const errors = [];
  const net = [];
  page.on('pageerror', e=>errors.push(e.message));
  page.on('request', req=>net.push({method:req.method(),url:req.url()}));
  const response = await page.goto(ROOT+'/', {waitUntil:'networkidle',timeout:25000});
  assert.ok(response, '主页没有 HTTP 响应');
  log('HTTP homepage', response.status());
  assert.equal(response.status(),200,'主页必须返回 HTTP 200');
  assert.match(await page.title(),/rnbatch/i,'线上不是 rnbatch 页面');
  assert.equal(await page.locator('#files').count(),1, '线上页面缺少文件输入控件');
  assert.equal(await page.locator('#preview').count(),1, '线上页面缺少预览控件');
  assert.equal(await page.locator('#download').count(),1, '线上页面缺少下载控件');
  log('Page source', 'rnbatch functional demo controls found');

  const js=await page.request.get(ROOT+'/app.js');
  const css=await page.request.get(ROOT+'/styles.css');
  assert.ok(js.ok() && css.ok(),'脚本或 CSS 静态资源失效');
  assert.match(js.headers()['content-type']||'',/javascript/i);
  assert.match(css.headers()['content-type']||'',/css/i);
  log('Static assets', 'JS '+js.status()+' CSS '+css.status());

  const fixture=Buffer.from('会议主题：飞桥改造项目\n项目名称：飞桥改造\n云端烟雾测试无隐私资料','utf8');
  await page.locator('#files').setInputFiles([
    {name:'会议纪要.txt',mimeType:'text/plain',buffer:fixture},
    {name:'IMG001.jpg',mimeType:'image/jpeg',buffer:Buffer.from([255,216,255,217])}
  ]);
  await page.getByText('2 个文件',{exact:true}).waitFor();
  await page.locator('#extract').click();
  await page.getByText('已从 1 份文档读取正文',{exact:false}).waitFor({timeout:12000});
  log('Text extraction', await page.locator('#suggestions').innerText());
  await page.locator('#keyword').fill('线上黑盒');
  await page.locator('#template').selectOption('{kw}-{n:02}');
  await page.locator('#preview').click();
  assert.equal(await page.locator('#preview-rows tr').count(),2);
  assert.equal(await page.locator('#download').isEnabled(),true);
  log('Rename preview','2 rows no conflict');
  const dlp=page.waitForEvent('download',{timeout:15000});
  await page.locator('#download').click();
  const download=await dlp;
  const path=await download.path();
  const bytes = await (await import('node:fs/promises')).readFile(path);
  const unpack=unzipSync(new Uint8Array(bytes));
  const names=Object.keys(unpack);
  assert.equal(names.length,2);
  assert.ok(names.some(x=>/^线上黑盒-\d+\.txt$/.test(x)));
  assert.ok(names.some(x=>/^线上黑盒-\d+\.jpg$/.test(x)));
  assert.equal(strFromU8(unpack[names.find(x=>x.endsWith('.txt'))]),fixture.toString('utf8'));
  log('ZIP', names.join(', '));

  // Detect whether the newest GitHub build has actually reached Cloudflare.
  // A PR may test the older live release; only scheduled runs enforce this.
  await page.locator('#reset').click();
  await page.locator('#files').setInputFiles({
    name:'first-batch.txt',mimeType:'text/plain',buffer:Buffer.from('a')
  });
  await page.locator('#files').setInputFiles({
    name:'second-batch.txt',mimeType:'text/plain',buffer:Buffer.from('b')
  });
  const deployedAppend = (await page.locator('#file-count').textContent()) === '2 个文件';
  log('Deployed incremental-import version',
      deployedAppend ? 'New additive file picker is live' : 'Older replace-file picker is still live');
  if (process.env.REQUIRE_ADDITIVE === '1') {
    assert.ok(deployedAppend,
      'Cloudflare site did not deploy the incremental-file import version from main');
  }

  // Live feature gate: PR runs may hit the previous Cloudflare release
  // before Pages finishes deployment. Nightly runs require new PDF support.
  const pdfAsset = await page.request.get(ROOT+'/pdf.worker.min.mjs');
  const pdfDeployed = pdfAsset.ok() &&
    /javascript/i.test(pdfAsset.headers()['content-type'] || '');
  log('PDF parser worker deployment',
    pdfDeployed ? 'matching PDF worker served from Cloudflare' :
      'PDF worker not yet deployed, HTTP '+pdfAsset.status());
  if (process.env.REQUIRE_PDF === '1')
    assert.ok(pdfDeployed,'Cloudflare Pages has not deployed pdf.worker.min.mjs');
  if (pdfDeployed) {
    const pdf = await PDFDocument.create();
    const pdfPage = pdf.addPage([612,792]);
    const font = await pdf.embedFont(StandardFonts.Helvetica);
    pdfPage.drawText('Project: Riverfront Renewal', {x:40,y:730,font,size:15});
    await page.locator('#reset').click();
    await page.locator('#files').setInputFiles({
      name:'live-extract.pdf',mimeType:'application/pdf',buffer:Buffer.from(await pdf.save())
    });
    await page.locator('#extract').click();
    await page.waitForFunction(()=>{
      const value=document.querySelector('#message')?.textContent||'';
      return /已从 1 份文档读取正文|没有检测到可读取的文字|读取文字失败/.test(value);
    },null,{timeout:30000});
    const pdfMessage=await page.locator('#message').innerText();
    assert.match(pdfMessage,/已从 1 份文档读取正文/);
    assert.match(await page.locator('#suggestions').innerText(),/Riverfront Renewal/);
    log('Live PDF text extraction','Success: Riverfront Renewal');
  }

  const nonStatic = net.filter(x=>!['GET','HEAD'].includes(x.method));
  assert.deepEqual(nonStatic,[],'页面向服务器发送了潜在的文件上传请求');
  assert.equal(errors.length,0,errors.join('; '));
  log('Browser security','No POST requests, no uncaught JS errors');

  // Mobile viewport / overflow and core workflow.
  // Firefox supports touch viewport emulation but Playwright's isMobile flag
  // is supported only by Chromium; do not confuse that with a real iOS test.
  const mobileOptions = browserName === 'firefox'
    ? {viewport:{width:390,height:844},hasTouch:true,deviceScaleFactor:3}
    : devices['iPhone 13'];
  const mobileCtx=await browser.newContext({...mobileOptions,acceptDownloads:true});
  const mobile=await mobileCtx.newPage();
  const mobileResp=await mobile.goto(ROOT+'/', {waitUntil:'domcontentloaded',timeout:20000});
  assert.equal(mobileResp?.status(),200);
  const dims=await mobile.evaluate(()=>({scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth}));
  assert.ok(dims.scroll<=dims.client+1,'移动页面产生水平滚动');
  await mobile.locator('#files').setInputFiles({name:'移动端.txt',mimeType:'text/plain',buffer:Buffer.from('移动端会议项目')});
  await mobile.locator('#keyword').fill('移动端');
  await mobile.locator('#preview').click();
  assert.equal(await mobile.locator('#download').isEnabled(),true);
  log('Mobile smoke','375px responsive layout and preview successful');
  await mobileCtx.close();
  await ctx.close();
  console.log('LIVE BLACKBOX PASS: '+findings.length+' checks ['+browserName+']');
} catch(e) {
  console.error('LIVE BLACKBOX FAILURE',e);
  process.exitCode=1;
} finally {
  writeFileSync(dest+'/result.json',JSON.stringify({url:ROOT,browser:browserName,status:process.exitCode?'FAILED':'PASSED',findings},null,2));
  await browser.close();
}
