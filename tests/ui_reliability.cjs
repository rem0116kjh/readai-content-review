/* Run with Playwright installed separately. Uses temporary data and fake AI. */
const {chromium} = require(process.env.READAI_PLAYWRIGHT_MODULE || 'playwright');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const output = process.argv[2] || path.join(root, '.local/reliability-20261020');
const python = `
import sys,tempfile,json
sys.path[:0]=[sys.argv[1],sys.argv[1]+'/tests']
import app,review
from test_review import fixture
from test_criteria import answer
def snapshot(key=None):
 s=fixture();s['fetched_at']='2020-01-01T00:00:00+09:00';s['source_mode']='snapshot'
 s['sheets']['5_문항']['rows'][0]['질문']=''
 return s
app.saved_sources=lambda:[{'id': 'fixture', 'label': '격리 시험 자료', 'fetched_at': '2020-01-01T00:00:00+09:00'}]
app.read_saved_source=snapshot
review.download_tab=lambda title,sid:snapshot()['sheets'][title]
review.codex_status=lambda:{'ready':True,'message':'격리 시험 AI'}
def fake(*args):
 if args[2].wait(0.5):raise review.Cancelled()
 return dict(answer(),rechecks=[])
review.ai_review_pass=fake
with tempfile.TemporaryDirectory() as folder:
 server=app.make_server(0,app.ReviewManager(folder,downloader=lambda *args:snapshot()))
 print(server.server_port,flush=True)
 try:server.serve_forever()
 finally:server.server_close()
`;

(async () => {
  fs.mkdirSync(output, {recursive: true});
  const child = spawn('python3', ['-u', '-c', python, root], {stdio: ['ignore','pipe','pipe']});
  const serverErrors = [];
  child.stderr.on('data', text => serverErrors.push(text.toString()));
  let browser;
  try {
    const port = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error('QA server startup timeout')), 10000);
      child.stdout.once('data', data => { clearTimeout(timer); resolve(Number(data.toString().trim())); });
      child.once('error', reject);
    });
    const base = `http://127.0.0.1:${port}`;
    browser = await chromium.launch({headless:true, executablePath:process.env.READAI_CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
    const context = await browser.newContext({viewport:{width:1440,height:1000}});
    const errors = [];
    context.on('page', page => page.on('pageerror', e => errors.push(e.message)));
    const page = await context.newPage();
    await page.goto(base);
    await page.waitForFunction(() => !document.getElementById('start').disabled);
    await page.locator('#source-select').selectOption('fixture');
    await page.waitForFunction(() => !document.getElementById('start').disabled);
    assert.match(await page.locator('#catalog-info').getAttribute('title'), /2020-01-01/);
    await page.locator('#set-select').selectOption('3-1-1-01');
    await page.locator('#refresh').click();
    await page.waitForFunction(() => !document.getElementById('start').disabled);
    assert.equal(await page.locator('#set-select').inputValue(),'3-1-1-01');
    const other = await context.newPage();
    await other.goto(base);
    await other.waitForFunction(() => !document.getElementById('start').disabled);

    await page.locator('#start').click();
    await page.waitForFunction(() => document.getElementById('run-info').textContent.includes('검토 완료'));
    assert.equal(await page.locator('.filter-tabs .active').getAttribute('data-filter'), 'error');
    assert.match(await page.locator('#progress-note').innerText(), /정순 1\/1 · 역순 1\/1/);
    await other.waitForFunction(() => document.getElementById('run-info').textContent.includes('검토 완료'), {timeout:15000});
    assert.equal(await other.locator('#count-errors').innerText(), await page.locator('#count-errors').innerText());
    const href = await page.locator('#export-json').getAttribute('href');
    const full = await (await context.request.get(base+href)).json();
    assert.equal(full.sets[0].ai_passes.forward.status, 'complete');
    assert.ok(full.sets[0].ai_passes.forward.result);
    const compact = await (await context.request.get(base+'/api/status')).json();
    assert.ok(!compact.job.sets[0].ai_passes.forward.result);
    assert.deepEqual(compact.job.issues, full.issues);

    // Deliberately reorder catalog responses: only the newer URL may win.
    await page.route('**/api/catalog?*', async route => {
      const url = route.request().url();
      if (url.includes('slow-sheet')) await new Promise(resolve => setTimeout(resolve, 700));
      const id = url.includes('slow-sheet') ? '3-stale' : '3-current';
      try {
        await route.fulfill({contentType:'application/json', body:JSON.stringify({
          sets:[{id,grade:3,title:id,sample:false,duplicate_rows:[]}], fetched_at:'2020-01-01'})});
      } catch (e) {
        if (!/closed|cancel|abort/i.test(e.message)) throw e;
      }
    });
    await page.locator('#source-select').selectOption('');
    await page.locator('#source-url').fill('https://docs.google.com/spreadsheets/d/slow-sheet-12345678901234567890/edit');
    await page.locator('#source-url').dispatchEvent('change');
    await page.locator('#source-url').fill('https://docs.google.com/spreadsheets/d/fast-sheet-12345678901234567890/edit');
    await page.locator('#source-url').dispatchEvent('change');
    await page.waitForFunction(() => document.querySelector('#set-select option[value="3-current"]'));
    await page.waitForTimeout(900);
    assert.equal(await page.locator('#set-select option[value="3-stale"]').count(),0);
    await page.unroute('**/api/catalog?*');
    await page.locator('#source-select').selectOption('fixture');
    await page.waitForFunction(() => !document.getElementById('start').disabled);
    await page.locator('#start').click();
    await page.waitForFunction(() => !document.getElementById('cancel').hidden);
    await page.locator('#cancel').click();
    await page.waitForFunction(() => document.getElementById('run-info').textContent.includes('중단됨'));
    await page.screenshot({path:path.join(output,'ui-reliability.png'),fullPage:true});
    assert.deepEqual(errors, []);
    assert.deepEqual(serverErrors, []);
    fs.writeFileSync(path.join(output,'ui-reliability.json'), JSON.stringify({
      checks:['snapshot timestamp','refresh preserves set','new run error filter','two pass completion',
        'other tab updates','full export history','compact poll parity','catalog race','cancel button'],
      page_errors:errors,server_errors:serverErrors,scope:'Isolated browser test, simulated AI; no live sheet or AI calls'
    }, null, 2));
    console.log('9 browser checks passed; no live sheet or AI calls.');
  } finally {
    if (browser) await browser.close();
    child.kill('SIGTERM');
  }
})().catch(error => {console.error(error);process.exitCode=1;});
