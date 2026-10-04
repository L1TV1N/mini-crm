// Дымовой тест интерфейса в настоящем браузере. Не входит в `unittest discover`, потому что нужен Playwright:
//   PLAYWRIGHT_CORE=/путь/к/node_modules/playwright-core node tests/ui_smoke.js http://127.0.0.1:8080 пароль
// Обходит все экраны в обеих темах и на телефоне, открывает карточку лида и ищет в видимом тексте мусор
// (null, undefined, NaN, [object Object]) и ошибки JavaScript. Такой мусор юнит-тесты на API не видят.
let pw;
try { pw = require(process.env.PLAYWRIGHT_CORE || 'playwright-core'); } catch { console.log('пропуск: playwright-core не найден'); process.exit(0); }
const BASE = (process.argv[2] || 'http://127.0.0.1:8080').replace(/\/$/, '');
const PASSWORD = process.argv[3] || 'admin';
const JUNK = /(^|[\s>(«"])(null|undefined|NaN|\[object Object\])($|[\s)<»".,:;])/;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await pw.chromium.launch({ executablePath: process.env.CHROMIUM || undefined, args: ['--no-sandbox'] });
  const problems = [];
  for (const [label, opts] of [['десктоп, светлая', { viewport: { width: 1440, height: 900 }, colorScheme: 'light' }],
                               ['десктоп, тёмная', { viewport: { width: 1440, height: 900 }, colorScheme: 'dark' }],
                               ['телефон', { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true }]]) {
    const ctx = await browser.newContext(opts);
    const r = await ctx.request.post(BASE + '/api/login', { data: { password: PASSWORD } });
    if (!r.ok()) { console.error('не удалось войти, проверьте пароль'); process.exit(2); }
    const page = await ctx.newPage();
    page.on('pageerror', (e) => problems.push(`${label}: ошибка JS: ${e.message}`));
    page.on('console', (m) => { if (m.type() === 'error' && !/401/.test(m.text())) problems.push(`${label}: консоль: ${m.text()}`); });
    for (const hash of ['#/leads', '#/leads?status=new', '#/leads?due=overdue', '#/board', '#/stats', '#/settings']) {
      await page.goto(`${BASE}/${hash}`, { waitUntil: 'networkidle' });
      await wait(600);
      const text = await page.evaluate(() => document.body.innerText);
      const m = text.split('\n').find((line) => JUNK.test(line));
      if (m) problems.push(`${label} ${hash}: в тексте мусор: «${m.trim().slice(0, 80)}»`);
      if (text.trim().length < 20) problems.push(`${label} ${hash}: страница пустая`);
    }
    // карточка первого лида
    await page.goto(`${BASE}/#/leads`, { waitUntil: 'networkidle' });
    await page.waitForSelector('.tr:not(.th), .empty', { timeout: 10000 });
    if (await page.$('.tr:not(.th)')) {
      await page.locator('.tr:not(.th)').first().click();
      await page.waitForSelector('.drawer', { timeout: 10000 });
      await wait(500);
      const text = await page.evaluate(() => document.querySelector('.drawer').innerText);
      const m = text.split('\n').find((line) => JUNK.test(line));
      if (m) problems.push(`${label} карточка: в тексте мусор: «${m.trim().slice(0, 80)}»`);
    }
    // публичная форма
    await page.goto(`${BASE}/form`, { waitUntil: 'networkidle' });
    if (JUNK.test(await page.evaluate(() => document.body.innerText))) problems.push(`${label} форма: в тексте мусор`);
    await ctx.close();
  }
  await browser.close();
  if (problems.length) { console.error('НАЙДЕНО ПРОБЛЕМ:', problems.length); problems.forEach((p) => console.error(' -', p)); process.exit(1); }
  console.log('интерфейс чистый: 3 режима × 6 экранов + карточка + форма, мусора и ошибок JS нет');
})();
