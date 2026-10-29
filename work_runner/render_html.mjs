/* Offline HTML print rendition; only installed inside the approved workspace image. */
import {chromium} from 'playwright-core';
import {writeFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import path from 'node:path';

const [source, output] = process.argv.slice(2);
if (!source || !output || !path.isAbsolute(source) || !path.isAbsolute(output)) {
  throw new Error('Expected absolute input and output paths');
}
let browser;
try {
  browser = await chromium.launch({executablePath:process.env.ATLAS_RENDER_CHROMIUM || '/usr/bin/chromium',
    headless:true,args:['--no-sandbox','--disable-dev-shm-usage','--no-zygote','--disable-background-networking'],timeout:30000});
  const context = await browser.newContext({offline:true,serviceWorkers:'block',acceptDownloads:false,
    viewport:{width:1440,height:1000},reducedMotion:'reduce'});
  await context.route('**/*', route => {
    const scheme = new URL(route.request().url()).protocol;
    return ['file:','data:','blob:'].includes(scheme) ? route.continue() : route.abort();
  });
  const page = await context.newPage();
  page.on('dialog', dialog => dialog.dismiss());
  await page.goto(pathToFileURL(source).href,{waitUntil:'load',timeout:30000});
  await page.emulateMedia({media:'print',reducedMotion:'reduce'});
  const result = await page.pdf({format:'A4',printBackground:true,preferCSSPageSize:true,timeout:30000});
  if (!result.length || result.length > 25*1024*1024) throw new Error('PDF size budget exceeded');
  await writeFile(output,result,{mode:0o600});
} catch (error) {
  process.stderr.write('HTML rendition failed ('+String(error.name||'Error').slice(0,50)+')\n');
  process.exitCode=1;
} finally {
  if (browser) await browser.close();
}
