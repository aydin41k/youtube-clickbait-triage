// Explicit, isolated visual QA with a FAKE classifier. Not inference evidence.
const fs=require("node:fs/promises"),path=require("node:path"),os=require("node:os");
const {chromium}=require("playwright");
const project=path.resolve(__dirname,"..");
(async()=>{
  const temporary=await fs.mkdtemp(path.join(os.tmpdir(),"yct-live-profile-"));
  const qa=await fs.mkdtemp(path.join(os.tmpdir(),"yct-live-corrected-qa-"));
  let context;
  try{
    context=await chromium.launchPersistentContext(path.join(temporary,"profile"),{headless:true,executablePath:process.env.TRIAGE_TEST_CHROMIUM||chromium.executablePath(),viewport:{width:1440,height:1000},env:{...process.env,XDG_CONFIG_HOME:path.join(temporary,"config")},args:[`--disable-extensions-except=${path.join(project,"extension")}`,`--load-extension=${path.join(project,"extension")}`,"--no-sandbox"]});
    const worker=context.serviceWorkers()[0]||await context.waitForEvent("serviceworker");const id=new URL(worker.url()).hostname;
    const hosts=path.join(temporary,"profile/NativeMessagingHosts");await fs.mkdir(hosts,{recursive:true});
    await fs.writeFile(path.join(hosts,"com.youtube_clickbait_triage.local.json"),JSON.stringify({name:"com.youtube_clickbait_triage.local",description:"FAKE-classifier live visual QA",type:"stdio",path:path.join(project,"tests/browser/run-fake-native-host"),allowed_origins:[`chrome-extension://${id}/`]}));
    const page=await context.newPage();const response=await page.goto("https://www.youtube.com/@veritasium/videos",{waitUntil:"domcontentloaded",timeout:45000});
    try { await page.waitForSelector('[data-yct-category="valid"]',{timeout:30000}); }
    catch(error){await page.screenshot({path:path.join(qa,"live-unavailable.png")});console.log(JSON.stringify({http:response.status(),qa,url:page.url(),snapshot:await page.evaluate(()=>({title:document.title,cards:document.querySelectorAll("yt-lockup-view-model").length,images:[...document.querySelectorAll("yt-lockup-view-model img")].slice(0,8).map(image=>({src:image.currentSrc||image.getAttribute("src"),width:image.getBoundingClientRect().width})),badges:[...document.querySelectorAll("[data-yct-badge]")].map(node=>node.textContent)}))}));throw error;}
    await page.waitForFunction(()=>[...document.querySelector("[data-yct-overlay]").shadowRoot.querySelectorAll(".frame")].some(node=>!node.hidden&&node.getBoundingClientRect().width>0));
    const state=await page.evaluate(()=>({bodyHeight:document.body.getBoundingClientRect().height,annotated:document.querySelectorAll("[data-yct-category]").length,frames:[...document.querySelector("[data-yct-overlay]").shadowRoot.querySelectorAll(".frame")].map(node=>({hidden:node.hidden,width:node.getBoundingClientRect().width,radius:node.style.borderRadius}))}));
    await page.screenshot({path:path.join(qa,"live-channel-fake-classifier.png")});
    await page.locator('[data-yct-category="valid"] a[href*="watch"]').first().hover();await page.waitForTimeout(1500);
    await page.screenshot({path:path.join(qa,"live-hover-fake-classifier.png")});
    console.log(JSON.stringify({http:response.status(),qa,...state}));
  }finally{await context?.close();await fs.rm(temporary,{recursive:true,force:true});}
})().catch(error=>{console.error(error);process.exitCode=1;});
