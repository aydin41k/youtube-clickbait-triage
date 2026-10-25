const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const { spawnSync } = require("node:child_process");
const { chromium } = require("playwright");
const project = path.resolve(__dirname, "../..");
const extension = path.join(project, "extension");
function card(id, title, video, portrait = false) {
  return `<ytd-rich-grid-media id="${id}" class="${portrait ? "portrait" : "landscape"}"><a id="media-${id}" class="media" href="/watch?v=${video}"><img src="https://i.ytimg.com/vi/${video}/hqdefault.jpg"><div class="preview"></div></a><h3><a id="video-title" href="/watch?v=${video}">${title}</a></h3></ytd-rich-grid-media>`;
}
const html = `<!doctype html><style>body{margin:24px;background:#f5f5f5;color:#222}ytd-rich-grid-media{display:inline-block;vertical-align:top;margin:0 24px 40px 0;position:relative;isolation:isolate;width:320px}.portrait{width:180px} .media{display:block;position:relative;height:180px;overflow:hidden;border-radius:12px;isolation:isolate}.portrait .media{height:320px}img,.preview{position:absolute;inset:0;width:100%;height:100%;display:block}.preview{z-index:9999;background:#253448;pointer-events:none}h3{font:16px system-ui;margin:8px 0}body.dark{background:#101010;color:#eee}</style>${card("red", "Clickbait positioned opaque thumbnail", "AAAAAAAAAAA")}${card("yellow", "Irrelevant portrait thumbnail", "BBBBBBBBBBB", true)}${card("grey", "Sensational preview thumbnail", "CCCCCCCCCCC")}${card("green", "Valid packaging", "DDDDDDDDDDD")}${card("none", "No majority", "EEEEEEEEEEE")}${card("unavailable", "Unavailable evidence", "FFFFFFFFFFF")}<script>window.clicks=0;document.addEventListener('click',e=>{if(e.target.closest('.media')){e.preventDefault();window.clicks++;}});</script>`;
function pixels(png, category) {
  const script = `import io,json,sys\nfrom PIL import Image\nim=Image.open(io.BytesIO(sys.stdin.buffer.read())).convert('RGB')\ncolors={'clickbait':(220,38,38),'irrelevant':(234,179,8),'sensational':(75,85,99),'valid':(22,163,74)}\nc=colors[sys.argv[1]]\ndef near(p): return max(abs(a-b) for a,b in zip(p,c))<12\nedge=[im.getpixel((x,y)) for x in range(24,im.width-24) for y in range(3)]\npoints=[im.getpixel((im.width//2,y)) for y in (1,5,10,17,im.height//2)]\nprint(json.dumps({'rim':sum(near(p) for p in edge),'samples':len(edge),'points':points,'center':im.getpixel((im.width//2,im.height//2))}))`;
  const result = spawnSync(path.join(project, ".venv/bin/python"), ["-c", script, category], { input: png });
  assert.equal(result.status, 0, result.stderr.toString()); return JSON.parse(result.stdout);
}
test("visible persistent inward-fading frames paint above positioned images and opaque high-z previews", { timeout: 60000 }, async () => {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "triage-frame-profile-"));
  const qa = await fs.mkdtemp(path.join(os.tmpdir(), "yct-overlay-qa-"));
  let context;
  try {
    const nativeEvents = path.join(temporary, "native-events");
    context = await chromium.launchPersistentContext(path.join(temporary, "profile"), { executablePath: process.env.TRIAGE_TEST_CHROMIUM || chromium.executablePath(), headless: true,
      viewport: { width: 1100, height: 850 }, env: { ...process.env, XDG_CONFIG_HOME: path.join(temporary, "config"), TRIAGE_TEST_NATIVE_EVENTS: nativeEvents }, args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`, "--no-sandbox"] });
    const worker = context.serviceWorkers()[0] || await context.waitForEvent("serviceworker"); const id = new URL(worker.url()).hostname;
    const hosts = path.join(temporary, "profile/NativeMessagingHosts"); await fs.mkdir(hosts, { recursive: true });
    await fs.writeFile(path.join(hosts, "com.youtube_clickbait_triage.local.json"), JSON.stringify({ name: "com.youtube_clickbait_triage.local", description: "isolated frame fixture", path: path.join(project, "tests/browser/run-fake-native-host"), type: "stdio", allowed_origins: [`chrome-extension://${id}/`] }));
    await context.route("https://www.youtube.com/**", (route) => route.fulfill({ contentType: "text/html", body: html }));
    await context.route("https://i.ytimg.com/**", (route) => route.fulfill({ contentType: "image/svg+xml", body: '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180"><rect width="320" height="180" fill="#253448"/></svg>' }));
    const page = await context.newPage(); await page.goto("https://www.youtube.com/"); await page.waitForSelector('#red[data-yct-category="clickbait"]');
    for (const [name, category] of [["red", "clickbait"], ["yellow", "irrelevant"], ["grey", "sensational"], ["green", "valid"]]) {
      const png = await page.locator(`#media-${name}`).screenshot({ path: path.join(qa, `light-${name}.png`) });
      const probe = pixels(png, category); assert.ok(probe.rim > probe.samples * 0.7, `${name}: colored rim must visibly overlay opaque thumbnail/preview: ${JSON.stringify(probe)}`);
      assert.deepEqual(probe.center, [37, 52, 72]);
      const distance = (point) => point.reduce((sum, value, index) => sum + Math.abs(value - probe.center[index]), 0);
      assert.ok(distance(probe.points[0]) > distance(probe.points[1]) && distance(probe.points[1]) > distance(probe.points[2]) && distance(probe.points[2]) > distance(probe.points[3]), "color alpha must fall inward, not obscure the center");
    }
    await page.screenshot({ path: path.join(qa, "light-page.png") });
    // YouTube positions its app absolutely: BODY has zero box height but its
    // overflow is propagated to the viewport when HTML overflow is visible.
    await page.evaluate(()=>{document.documentElement.style.overflow="visible";document.body.style.overflowY="scroll";const app=document.createElement("div");app.id="absolute-app";app.style.cssText="position:absolute;left:24px;top:24px;width:1050px";for(const card of [...document.querySelectorAll("ytd-rich-grid-media")])app.append(card);document.body.append(app);});
    await page.waitForTimeout(80);
    assert.equal(await page.evaluate(()=>document.body.getBoundingClientRect().height),0);
    const propagated=pixels(await page.locator("#media-red").screenshot(),"clickbait");assert.ok(propagated.rim>propagated.samples*.7,"viewport-propagated BODY overflow must not clip frames to its zero-height box");
    // A genuine zero-height container still clips, unlike propagated BODY.
    await page.locator("#red").evaluate(node=>{const clip=document.createElement("div");clip.id="real-zero-clip";clip.style.cssText="height:0;overflow:hidden";node.replaceWith(clip);clip.append(node);});
    await page.waitForFunction(()=>document.querySelector("[data-yct-overlay]").shadowRoot.querySelector('[data-yct-frame="clickbait"]').hidden);
    await page.locator("#real-zero-clip").evaluate(node=>node.replaceWith(node.firstElementChild));
    await page.waitForFunction(()=>!document.querySelector("[data-yct-overlay]").shadowRoot.querySelector('[data-yct-frame="clickbait"]').hidden);
    await page.locator("#media-red").evaluate(node=>{node.style.borderRadius="0";const wrapper=document.createElement("yt-thumbnail-view-model");wrapper.style.cssText="display:block;position:absolute;inset:0;overflow:hidden;border-radius:12px";while(node.firstChild)wrapper.append(node.firstChild);node.append(wrapper);});
    await page.waitForFunction(()=>document.querySelector("[data-yct-overlay]").shadowRoot.querySelector('[data-yct-frame="clickbait"]').style.borderRadius==="12px");
    await page.evaluate(() => document.body.classList.add("dark")); await page.waitForTimeout(150);
    await page.screenshot({ path: path.join(qa, "dark-page.png") });
    for (const name of ["none", "unavailable"]) assert.equal(await page.locator(`#${name}`).getAttribute("data-yct-category"), null);
    const media = page.locator("#media-red"); await media.click({ position: { x: 3, y: 3 } }); await media.click({ position: { x: 160, y: 90 } }); assert.equal(await page.evaluate(() => window.clicks), 2);
    const before = await fs.readFile(nativeEvents, "utf8");
    const aligned = async () => page.waitForFunction(() => { const image = document.querySelector("#media-red img").getBoundingClientRect(); const frame = document.querySelector("[data-yct-overlay]").shadowRoot.querySelector('[data-yct-frame="clickbait"]')?.getBoundingClientRect(); return frame && Math.abs(frame.left-image.left)<1 && Math.abs(frame.top-image.top)<1 && Math.abs(frame.width-image.width)<1; });
    await media.evaluate((node) => { node.style.transform = "translateX(21px)"; }); await aligned();
    await page.locator("#red").evaluate((node) => { node.classList.add("moved"); const style=document.createElement("style"); style.textContent="#red.moved{transform:translateY(13px)}"; document.head.append(style); }); await aligned();
    await page.locator("#media-red img").evaluate((node) => node.replaceWith(node.cloneNode())); await aligned();
    await page.setViewportSize({ width:900,height:650 }); await aligned();
    await page.evaluate(() => window.scrollTo(0,80)); await aligned();
    assert.equal(await fs.readFile(nativeEvents, "utf8"), before, "same-key geometry/DOM changes must not dispatch a new analysis");
    const persistent = pixels(await media.screenshot(), "clickbait"); assert.ok(persistent.rim > persistent.samples*0.7, "rim stays visible after delay/layout changes");
    const removedImmediately = await page.evaluate(() => new Promise((resolve) => {
      document.querySelector("#red #video-title").textContent="No majority recycled thumbnail";
      queueMicrotask(()=>resolve(!document.querySelector("[data-yct-overlay]").shadowRoot.querySelector('[data-yct-frame="clickbait"]')));
    })); assert.ok(removedImmediately,"changed packaging removes old frame before debounced requeue");
    await page.waitForFunction(()=>document.querySelector("#red [data-yct-badge]").textContent.includes("above 50%"));
    console.log(`TEMP QA screenshots (no business data): ${qa}`);
  } finally { await context?.close(); await fs.rm(temporary, { recursive: true, force: true }); }
});
