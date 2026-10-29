import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {fileURLToPath} from 'node:url';
import {chromium as playwright} from 'playwright-core';
const root=fileURLToPath(new URL('../../agent_service/static',import.meta.url));
const evidenceDir=process.env.ATLAS_BROWSER_EVIDENCE_DIR||fs.mkdtempSync(path.join(os.tmpdir(),'atlas-browser-check-'));fs.mkdirSync(evidenceDir,{recursive:true});
if(!process.env.ATLAS_BROWSER_EXECUTABLE)throw Error('Set ATLAS_BROWSER_EXECUTABLE to an installed Chromium binary; this test never downloads browsers.');
const project={id:'p',name:'Work validation fixture',revision:1},session={id:'c',name:'Report review',revision:1};
const aid='wa_'+'a'.repeat(44),renditionId='waf_'+'c'.repeat(48);let image;
const item={id:aid,name:'report.html',kind:'work',format:'html',sha256:'b'.repeat(64),updated_at:'2026-10-29T04:00:00Z',preview_url:'/api/projects/p/artifacts/'+aid+'/preview',download_url:'/api/projects/p/artifacts/'+aid+'/download'};
const calls=[],errors=[];
const browser=await playwright.launch({executablePath:process.env.ATLAS_BROWSER_EXECUTABLE,args:['--no-sandbox','--disable-dev-shm-usage','--no-zygote'],headless:true});
try{
 const fixture=await browser.newPage({viewport:{width:1024,height:720}});await fixture.setContent('<!doctype html><style>body{font:24px sans-serif;padding:40px;color:#123}h1{color:#17694c}.cards{display:flex;gap:20px}.card{border:1px solid #bbb;border-radius:12px;padding:20px}</style><h1>Work report preview</h1><p>Controlled fixture, no business approval.</p><div class=cards><div class=card>Research</div><div class=card>118.1500</div><div class=card>Needs review</div></div>');image=await fixture.screenshot();await fixture.close();
 const context=await browser.newContext({viewport:{width:1440,height:1000},serviceWorkers:'block'});
 if(process.env.ATLAS_BROWSER_FONT_PACKAGE){
  const folder=process.env.ATLAS_BROWSER_FONT_PACKAGE,css=fs.readFileSync(path.join(folder,'400.css'),'utf8');
  const fonts=[...css.matchAll(/@font-face\s*\{([\s\S]*?)\}/g)].map(match=>{
   const file=match[1].match(/url\(\.\/([^)]*\.woff2)\)/)?.[1],range=match[1].match(/unicode-range:\s*([^;]+);/)?.[1];
   if(!file||!range)throw Error('Invalid optional fixture font package');
   return {bytes:fs.readFileSync(path.join(folder,file)).toString('base64'),range};
  });
  await context.addInitScript(async fonts=>{
   await Promise.all(fonts.map(async value=>{const bytes=Uint8Array.from(atob(value.bytes),c=>c.charCodeAt(0));const font=await new FontFace('Microsoft YaHei',bytes,{weight:'400',unicodeRange:value.range}).load();document.fonts.add(font)}));
   window.__atlasFixtureFontsReady=true;
  },fonts);
 }
 await context.addInitScript(()=>sessionStorage.setItem('atlas_token','controlled-ui-fixture'));
 const page=await context.newPage();
 page.on('pageerror',error=>errors.push(error.message));
 await page.route('**/*',async route=>{
  const url=new URL(route.request().url());
  if(url.origin!=='https://atlas.fixture')return route.abort();
  const p=url.pathname;
  if(p.startsWith('/api/')){
   calls.push(p);
   if(p.endsWith('/work-artifacts/'+renditionId))return route.fulfill({status:200,contentType:'image/png',body:image});
   let value={};
   if(p==='/api/models')value={models:[{id:'fixture',name:'Controlled fixture'}],default_model_id:'fixture'};
   else if(p==='/api/projects')value=[project];
   else if(p==='/api/projects/p/sessions')value=[session];
   else if(p.endsWith('/conversation'))value={project,session_id:'c',sessions:[session],sources:[],applications:[],turns:[],mode:'llm'};
   else if(p==='/api/projects/p/artifacts')value={items:[item],total:1,next_offset:null};
   else if(p.endsWith('/preview'))value={render:'text',text:'<script>window.fixtureUnsafe=true</script><h1>Report</h1>',visual_preview:{total_pages:3,rendered_pages:2,complete:false,visual_inspected:false,images:[{page:1,download_url:'/api/projects/p/work-artifacts/'+renditionId},{page:2,download_url:'/api/projects/p/work-artifacts/'+renditionId}]}};
   else if(p==='/api/projects/p/work-environment')value={configured:true,web_research:{page_reading:true,search_configured:true},vision:{enabled:true},executions:[]};
   else if(p==='/api/sources'||p.endsWith('/applications'))value=[];
   return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(value)});
  }
  const file=path.join(root,p==='/'?'index.html':p.slice(1));
  if(!file.startsWith(root+'/')||!fs.existsSync(file))return route.fulfill({status:404,body:'Not found'});
  const type=file.endsWith('.html')?'text/html':file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'application/octet-stream';
  return route.fulfill({status:200,contentType:type,body:fs.readFileSync(file),headers:{'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data: blob:; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'"}});
 });
 await page.goto('https://atlas.fixture/?project=p');
 if(process.env.ATLAS_BROWSER_FONT_PACKAGE)await page.waitForFunction(()=>window.__atlasFixtureFontsReady);
 await page.waitForSelector('#shell:not([hidden])',{timeout:15000});
 await page.click('#workspace-artifacts');
 await page.click('[data-preview-artifact="'+aid+'"]');
 await page.waitForSelector('.artifact-rendition img[src^="blob:"]');
 await page.waitForFunction(()=>document.querySelector('.artifact-rendition img')?.naturalWidth>0);
 if(!await page.locator('.artifact-rendition').textContent().then(t=>t.includes('仅生成前2页')))throw Error('Missing partial-page disclosure');
 if(await page.evaluate(()=>!!window.fixtureUnsafe))throw Error('Generated script ran');
 await page.getByRole('button',{name:'下一页',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('.artifact-rendition img')?.alt.includes('第2页'));
 await page.screenshot({path:path.join(evidenceDir,'desktop-rendered-preview.png')});
 const desktopOverflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
 await page.setViewportSize({width:390,height:844});
 await page.screenshot({path:path.join(evidenceDir,'mobile-rendered-preview.png')});
 const mobileOverflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
 if(desktopOverflow||mobileOverflow)throw Error('Horizontal page overflow');
 if(errors.length)throw Error('Page errors: '+JSON.stringify(errors));
 console.log(JSON.stringify({real_chromium:browser.version(),mock_api:true,no_live_model_or_deployment:true,desktop_preview:true,mobile_preview:true,png_decoded:true,pagination:true,generated_script_not_executed:true,partial_pages_disclosed:true,no_horizontal_overflow:true,evidence_directory:evidenceDir}));
} catch(error){console.error(JSON.stringify({error:error.message,calls,errors}));process.exitCode=1}
finally{await browser.close()}
