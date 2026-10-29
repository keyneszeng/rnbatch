// Run against a *separate Python process* serving the actual Web App.
// Browser-facing E2E only: navigate, type, upload, preview, apply, download, undo.
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

const BASE = process.env.E2E_BASE || 'http://127.0.0.1:8766';
const PASSWORD_A = process.env.E2E_PASSWORD_A || 'BlackboxStrongPass_2026_A!';
const PASSWORD_B = process.env.E2E_PASSWORD_B || 'BlackboxStrongPass_2026_B!';
const tmp = mkdtempSync(join(tmpdir(), 'rnbatch-e2e-'));
const stats = [];
const ok = name => { stats.push({ name, pass: true }); console.log('PASS  ' + name); };
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });

async function login(page, username, password) {
  await page.goto(BASE + '/');
  await page.locator('#username').fill(username);
  await page.locator('#password').fill(password);
  await page.locator('#submit').click();
  await page.waitForURL(BASE + '/');
  await page.locator('#uploads').waitFor();
}

try {
  const ctx = await browser.newContext({ acceptDownloads: true });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));

  await page.goto(BASE + '/');
  assert.equal(await page.locator('#username').count(), 1);
  assert.equal(await page.locator('#password').count(), 1);
  ok('未登录用户看到登录页');

  await page.locator('#username').fill('alice');
  await page.locator('#password').fill('incorrect-password');
  await page.locator('#submit').click();
  await page.getByText('用户名或密码不正确').waitFor();
  ok('错误密码被拒绝且展示错误');

  await login(page, 'alice', PASSWORD_A);
  assert.equal(await page.getByText('已登录：alice').count(), 1);
  ok('真实浏览器登录并保持会话');

  const legacy = await page.evaluate(async () => {
    const r = await fetch('/api/list', {
      method: 'POST', headers: {
        'Content-Type': 'application/json', 'X-RNBatch-CSRF': CSRF
      },
      body: JSON.stringify({ path: '/etc' })
    });
    return r.status;
  });
  assert.equal(legacy, 404);
  ok('旧本地目录接口不允许读取服务器文件');

  const folder = join(tmp, 'files');
  // Playwright can populate native file inputs from buffers without special permissions.
  const text = Buffer.from('星海中心项目 第一次启动会议记录\n项目编号：XY-2026\n参会：项目管理小组\n', 'utf8');
  const image = Buffer.from([0xff, 0xd8, 0xff, 0xd9]);
  const created = page.waitForResponse(resp => resp.url().includes('/api/workspaces/create')
                                                && resp.request().method() === 'POST');
  await page.locator('#files').setInputFiles([
    { name: '会议纪要.txt', mimeType: 'text/plain', buffer: text },
    { name: 'IMG_001.jpg', mimeType: 'image/jpeg', buffer: image }
  ]);
  const createdResp = await created;
  assert.equal(createdResp.status(), 200);
  const { workspace: wid } = await createdResp.json();
  assert.ok(wid?.length > 20);
  await page.getByText('2 个文件上传完成', {exact:false}).waitFor({ timeout: 20000 });
  ok('浏览器选择两个文件并完成隔离上传');

  const traversal = await page.evaluate(async sid => {
    const qs = new URLSearchParams({workspace:sid, name:'../outside.txt'});
    const r = await fetch('/api/workspaces/upload?' + qs.toString(), {
      method:'POST',headers:{'X-RNBatch-CSRF':CSRF},body:new Blob(['pwn'])
    });
    return r.status;
  }, wid);
  assert.equal(traversal, 400);
  ok('目录穿越文件名被 HTTP 上传接口拒绝');

  const cookies = await ctx.cookies();
  const token = cookies.find(c => c.name === 'rnbatch_session')?.value;
  assert.ok(token);
  const noCsrf = await fetch(BASE + '/api/workspaces/create', {
    method:'POST',headers:{
      'Content-Type':'application/json','Origin':BASE,
      'Cookie':'rnbatch_session='+token,
      'X-RNBatch-CSRF':'invalid-token'
    },body:'{}'
  });
  assert.equal(noCsrf.status, 403);
  ok('已登录但 CSRF 错误的请求被拒绝');

  const csrf = await page.evaluate(() => CSRF);
  const foreignOrigin = await fetch(BASE + '/api/workspaces/create', {
    method:'POST',headers:{
      'Content-Type':'application/json','Origin':'https://untrusted.example',
      'Cookie':'rnbatch_session='+token,
      'X-RNBatch-CSRF': csrf
    },body:'{}'
  });
  assert.equal(foreignOrigin.status, 403);
  ok('伪造其他 Origin 的请求被拒绝');

  await page.locator('#extract').click();
  await page.getByText('提取完成', {exact:false}).waitFor({timeout:20000});
  await page.locator('#keyword').fill('黑盒项目');
  await page.locator('#template').selectOption('{kw}-{n:02}');
  await page.locator('#preview').click();
  await page.getByText('2 个文件，将修改 2 个，冲突 0 个').waitFor();
  assert.equal(await page.locator('#rows tr').count(), 2);
  assert.equal(await page.locator('#apply').isEnabled(), true);
  ok('文档提取、选择关键词、预览两行且无冲突');

  const withoutToken = await page.evaluate(async sid => {
    const r = await fetch('/api/workspaces/apply', {
      method:'POST',headers:{
        'Content-Type':'application/json','X-RNBatch-CSRF':CSRF
      },body:JSON.stringify({workspace:sid,plan_token:'not-the-preview-token'})
    });
    return r.status;
  }, wid);
  assert.equal(withoutToken, 400);
  ok('未经匹配预览确认的执行被拒绝');

  const ctxB = await browser.newContext({acceptDownloads: true});
  const bob = await ctxB.newPage();
  await login(bob, 'bob', PASSWORD_B);
  const other = await bob.evaluate(async wid => {
    const res = await fetch('/api/workspaces/list', {
      method:'POST', headers:{
        'Content-Type':'application/json', 'X-RNBatch-CSRF':CSRF
      }, body: JSON.stringify({workspace: wid})
    });
    return res.status;
  }, wid);
  assert.equal(other, 400);
  const otherZip = await bob.goto(BASE + '/api/workspaces/download?workspace=' + encodeURIComponent(wid));
  assert.equal(otherZip.status(), 400);
  ok('另一账户不能读取工作区或下载 ZIP');

  page.once('dialog', d => d.accept());
  await page.locator('#apply').click();
  await page.getByText('已整理 2 个文件').waitFor();
  assert.equal(await page.locator('#download').isEnabled(), true);
  ok('确认弹窗 + 执行改名成功');

  const downloadPromise = page.waitForEvent('download');
  await page.locator('#download').click();
  const download = await downloadPromise;
  const downloadPath = await download.path();
  assert.equal(download.suggestedFilename(), 'rnbatch-files.zip');
  const inspect = execFileSync('python', [
    '-c',
    'import json,sys,zipfile; z=zipfile.ZipFile(sys.argv[1]); print(json.dumps({"n":z.namelist(),"txt":[z.read(x).decode("utf-8") for x in z.namelist() if x.endswith(".txt")]}))',
    downloadPath
  ], { encoding: 'utf8' });
  const zip = JSON.parse(inspect);
  assert.equal(zip.n.length, 2);
  assert.ok(zip.n.some(n => /黑盒项目-\d+\.txt$/.test(n)));
  assert.ok(zip.n.some(n => /黑盒项目-\d+\.jpg$/.test(n)));
  assert.ok(zip.txt[0].includes('星海中心项目'));
  ok('浏览器实际下载 ZIP，文件名和源文件内容正确');

  await page.locator('#undo').click();
  await page.getByText('恢复 2 个文件，跳过 0 个').waitFor();
  assert.equal(await page.locator('#preview').isEnabled(), true);
  await page.locator('#preview').click();
  await page.getByText('2 个文件，将修改 2 个，冲突 0 个').waitFor();
  assert.ok((await page.locator('#rows').innerText()).includes('会议纪要.txt'));
  ok('Web UI 撤销成功且原文件名恢复');

  await page.locator('#remove').click();
  await page.getByText('文件已从当前临时工作区删除').waitFor();
  ok('浏览器主动删除临时工作区');

  await page.locator('#logout').click();
  await page.locator('#username').waitFor();
  ok('浏览器正常注销并返回登录页');

  assert.deepEqual(errors, []);
  ok('整个流程没有前端未捕获 JavaScript 错误');

  await ctxB.close();
  await ctx.close();
  console.log('\\nBLACKBOX SUCCESS: ' + stats.length + ' browser scenarios passed.');
} catch (error) {
  console.error('BLACKBOX FAILURE:', error);
  process.exitCode = 1;
} finally {
  await browser.close();
  rmSync(tmp, {recursive:true,force:true});
}
