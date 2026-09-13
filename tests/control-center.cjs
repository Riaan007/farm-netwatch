const { chromium }=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs=require('fs');
const assert=require('node:assert/strict');
const path=require('node:path');
const root=path.resolve(__dirname,'..');
const file=path.join(root,'hub/app/static/control-center.html');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH || '/usr/bin/chromium',headless:true,args:['--no-sandbox','--disable-dev-shm-usage','--disable-gpu']});
 const page=await browser.newPage({viewport:{width:1512,height:1100}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto('file://'+file);await page.getByText('Interactive preview',{exact:true}).waitFor();
 assert.equal(await page.locator('#metrics .metric strong').nth(1).textContent(),'12');
 await page.screenshot({path:path.join(root,'docs/control-center-desktop.png'),fullPage:true});
 await page.locator('.nav [data-view="mikrotik"]').click();assert.equal(await page.locator('#workspace tbody tr').count(),4);
 await page.locator('#workspace [data-asset]').first().click();assert(await page.locator('#drawer').isVisible());assert(await page.getByRole('button',{name:'SSH integration not connected'}).isDisabled());await page.keyboard.press('Escape');
 await page.locator('.nav [data-view="assets"]').click();await page.locator('#search').fill('Yard');assert.equal(await page.locator('#workspace tbody tr').count(),1);
 const downloadPromise=page.waitForEvent('download');await page.locator('#export').click();const download=await downloadPromise;assert(download.suggestedFilename().includes('example'));
 await page.locator('#search').fill('');await page.locator('#status-filter').selectOption('unknown');assert.equal(await page.locator('#workspace tbody tr').count(),2);await page.locator('#status-filter').selectOption('');
 await page.locator('.nav [data-view="backups"]').click();assert.equal(await page.locator('#workspace tbody tr').count(),4);
 await page.locator('.nav [data-view="overview"]').click();await page.setViewportSize({width:390,height:844});
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:path.join(root,'docs/control-center-mobile.png'),fullPage:true});
 // Live mode: API contract, managed scope, duplicate IPs, stale state and session failure.
 const live=await browser.newPage({viewport:{width:1440,height:1000}});live.on('pageerror',e=>errors.push(e.message));
 let fail=false,failAssets=false;const now=Math.floor(Date.now()/1000);const sites=[{id:'a',name:'Client A',reachable:true,fetched_at:now,pi_health:'ok'},{id:'b',name:'Client B',reachable:false,stale:true,fetched_at:now-900}];
 await live.route('http://netwatch.test/**',async route=>{
 const path=new URL(route.request().url()).pathname;
 assert.equal(route.request().method(),'GET','Control center must not mutate clients');
 if(path==='/control-center')return route.fulfill({contentType:'text/html',body:fs.readFileSync(file,'utf8')});
 if(fail)return route.fulfill({status:401,contentType:'application/json',body:'{}'});
 let body;
 if(path==='/api/hub/overview')body={sites};
 else if(path.endsWith('/devices')){const id=path.split('/')[4];if(failAssets&&id==='a')return route.fulfill({status:502,body:'{}'});body={card:sites.find(s=>s.id===id),devices:[{key:'router',name:'<img src=x onerror=alert(1)>',category:'network',watch:true,online:true,vendor:'MikroTik',ip:'192.168.88.1'},{key:'phone',name:'Private phone',category:'phone',watch:true,online:true},{key:'unapproved',name:'Unapproved camera',category:'camera',watch:false,online:true},{key:'printer',category:'printer',watch:true,online:true}],stale:id==='b'};}
 else if(path.endsWith('/backups'))body={backups:[]};else return route.fulfill({status:404,body:'{}'});
 return route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
 });
 await live.goto('http://netwatch.test/control-center');await live.getByText('Hub snapshots',{exact:true}).waitFor();
 assert.equal(await live.locator('#metrics .metric strong').nth(1).textContent(),'2');assert.equal(await live.locator('#metrics .metric strong').nth(3).textContent(),'1');
 await live.locator('.nav [data-view="assets"]').click();assert.equal(await live.locator('#workspace tbody tr').count(),2);assert.equal(await live.locator('#workspace img').count(),0);assert.equal(await live.getByText('Private phone',{exact:true}).count(),0);
 await live.locator('#workspace [data-asset]').nth(2).click();assert((await live.locator('#drawer-body').textContent()).includes('Client B'));await live.keyboard.press('Escape');
 failAssets=true;await live.locator('#refresh').click();await live.getByText(/site inventories unavailable/).waitFor();assert.equal(await live.locator('#metrics .metric strong').nth(3).textContent(),'2');
 fail=true;await live.locator('#refresh').click();await live.getByText(/Session expired/).waitFor();assert.equal(await live.locator('#metrics .metric strong').nth(0).textContent(),'0 / 2');assert.equal(await live.locator('#metrics .metric strong').nth(3).textContent(),'2');
 assert.deepEqual(errors,[]);await browser.close();console.log('PASS: desktop/mobile, preview navigation, filtering, CSV, dialog, backups, managed scope, XSS escaping, overlapping addresses, stale data, partial failures, session expiry, GET-only behavior.');
})().catch(e=>{console.error(e);process.exit(1)});
