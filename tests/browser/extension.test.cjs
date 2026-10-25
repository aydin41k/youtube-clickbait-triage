const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const { chromium } = require("playwright");
const project = path.resolve(__dirname, "../..");
const extension = path.join(project, "extension");
test("isolated real extension: YouTube content -> worker -> framed companion -> four borders and dynamic cards", { timeout: 60000 }, async () => {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "triage-chromium-"));
  let context;
  try {
    const nativeEvents = path.join(temporary, "native-events");
    const executable = process.env.TRIAGE_TEST_CHROMIUM || chromium.executablePath();
    context = await chromium.launchPersistentContext(path.join(temporary, "profile"), { executablePath: executable, headless: true,
      ignoreDefaultArgs: ["--disable-back-forward-cache"],
      env: { ...process.env, XDG_CONFIG_HOME: path.join(temporary, "config"), TRIAGE_TEST_NATIVE_EVENTS: nativeEvents, TRIAGE_TEST_SLOW_GATE: path.join(temporary, "release-slow") },
      args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`, "--no-sandbox"] });
    const worker = context.serviceWorkers()[0] || await context.waitForEvent("serviceworker");
    const identifier = new URL(worker.url()).hostname;
    // Chromium resolves its user-level host directory under overridden DIR_USER_DATA.
    const hostDirectory = path.join(temporary, "profile/NativeMessagingHosts");
    await fs.mkdir(hostDirectory, { recursive: true });
    const fixture = await fs.readFile(path.join(project, "tests/fixtures/cards.html"), "utf8");
    const trackedFixture = fixture.replace("<head>", '<head><script>window.__lastPageShowPersisted=false;window.addEventListener("pageshow",event=>{window.__lastPageShowPersisted=event.persisted;});</script>');
    await context.route("https://www.youtube.com/**", (route) => route.fulfill({ contentType: "text/html", body: trackedFixture }));
    await context.route("https://example.test/**", (route) => route.fulfill({ contentType: "text/html", body: "<!doctype html><title>Other test page</title><p>Navigation target</p>" }));
    await context.route("https://i.ytimg.com/**", (route) => route.fulfill({ contentType: "image/svg+xml", body: '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"><rect width="200" height="100" fill="grey"/></svg>' }));
    const page = await context.newPage(); await page.goto("https://www.youtube.com/");
    await page.waitForFunction(() => document.querySelector("[data-yct-badge]")?.textContent.includes("local_host_disconnected"));
    await fs.writeFile(path.join(hostDirectory, "com.youtube_clickbait_triage.local.json"), JSON.stringify({ name: "com.youtube_clickbait_triage.local", description: "isolated fixture companion", path: path.join(project, "tests/browser/run-fake-native-host"), type: "stdio", allowed_origins: [`chrome-extension://${identifier}/`] }));
    const popup = await context.newPage(); await popup.goto(`chrome-extension://${identifier}/popup.html`);
    await popup.locator("#retry").click();
    try { await page.waitForSelector('[data-yct-category="valid"]', { timeout: 15000 }); }
    catch (error) {
      console.log("worker state", await worker.evaluate(() => scheduler.status()), "badges", await page.locator("[data-yct-badge]").allTextContents());
      console.log("native error", await worker.evaluate(() => new Promise((resolve) => { const port = chrome.runtime.connectNative("com.youtube_clickbait_triage.local"); const timeout = setTimeout(() => { port.disconnect(); resolve("diagnostic timeout"); }, 1000); port.onDisconnect.addListener(() => { clearTimeout(timeout); resolve(chrome.runtime.lastError?.message); }); })));
      throw error;
    }
    const expected = { home: ["clickbait", "rgb(220, 38, 38)"], search: ["irrelevant", "rgb(234, 179, 8)"], channel: ["sensational", "rgb(75, 85, 99)"], recommendation: ["valid", "rgb(22, 163, 74)"] };
    for (const [id, [category, color]] of Object.entries(expected)) {
      await page.waitForSelector(`#${id}[data-yct-category="${category}"]`);
      assert.ok((await page.locator(`[data-yct-frame="${category}"]`).first().evaluate((node) => getComputedStyle(node).boxShadow)).includes(color));
    }
    assert.equal(await page.locator("[data-yct-category]").count(), 8);
    assert.equal(await page.locator("#home #video-title").getAttribute("aria-label"), "Original native label");
    assert.equal(await page.locator("#collection").getAttribute("data-yct-category"), null);
    const runsPromise = context.waitForEvent("page"); await popup.locator("#runs").click(); const runs = await runsPromise;
    await runs.waitForSelector("#recent .attempt");
    const qa = await fs.mkdtemp(path.join(os.tmpdir(), "yct-results-qa-"));
    assert.ok((await runs.locator("body").textContent()).includes("Local analysis attempts, not guaranteed Gemma inference"));
    assert.equal(await runs.locator("#recent .scored").count(), 8);
    assert.equal(await runs.locator("#recent .interrupted").count(), 3, "initial three missing-host dispatches are interrupted attempts, not completed model runs");
    assert.ok((await runs.locator("#recent").textContent()).includes("total analysis duration"));
    assert.equal(await runs.locator("#recent .attempt").first().locator("dt").count(), 4);
    const recordsBeforeRetry = await runs.locator("#recent .attempt").count();
    await page.evaluate(() => { for (let n = 0; n < 4; n++) { const card=document.querySelector("#recommendation").cloneNode(true);card.id=`slow-${n}`;card.querySelector("#video-title").textContent=`Slow local analysis attempt ${n}`;document.body.append(card); } });
    await runs.waitForSelector("#current .running"); await runs.waitForSelector("#current .queued");
    assert.equal(await runs.locator("#current .running").count(), 3);
    assert.ok((await runs.locator("#connection").textContent()).includes("3 active attempts"));
    assert.ok((await runs.locator("#current").textContent()).includes("Queued — not run"));
    await runs.screenshot({path:path.join(qa,"current-work.png"),fullPage:true});
    await runs.screenshot({path:path.join(qa,"current-viewport.png")});
    await fs.writeFile(path.join(temporary, "release-slow"), "release");
    await page.locator("#home #video-title").evaluate((node) => { node.title = "No majority changed packaging"; node.textContent = node.title; });
    await page.waitForFunction(() => document.querySelector("#home [data-yct-badge]").textContent.includes("above 50%"));
    assert.equal(await page.locator("#home").getAttribute("data-yct-category"), null);
    await page.evaluate(() => { const card = document.querySelector("#recommendation").cloneNode(true); card.id = "dynamic"; card.querySelector("#video-title").textContent = "Unavailable <script>bad</script>"; document.body.append(card); document.dispatchEvent(new Event("yt-navigate-finish")); });
    await page.waitForFunction(() => document.querySelector("#dynamic [data-yct-badge]")?.textContent.includes("no_english_captions"));
    assert.equal(await page.locator("#dynamic").getAttribute("data-yct-category"), null);
    await runs.waitForFunction(() => document.querySelector("#recent").textContent.includes("no_english_captions"));
    await runs.screenshot({ path: path.join(qa,"light-results.png"), fullPage:true });
    await runs.screenshot({path:path.join(qa,"light-viewport.png")});
    await runs.emulateMedia({ colorScheme:"dark" }); await runs.screenshot({ path:path.join(qa,"dark-results.png"),fullPage:true });
    await runs.screenshot({path:path.join(qa,"dark-viewport.png")});
    assert.equal(await runs.locator("#recent script").count(),0,"hostile displayed title remains inert text");
    await popup.locator("#pause").click(); await page.waitForFunction(() => !document.querySelector("[data-yct-category]"));
    assert.equal(await worker.evaluate(async () => (await chrome.storage.session.get("triagePaused")).triagePaused), true);
    const eventsBeforeStop = await fs.readFile(nativeEvents, "utf8");
    const cdp = await context.newCDPSession(page);
    const versions = new Map();
    cdp.on("ServiceWorker.workerVersionUpdated", (event) => { for (const version of event.versions) versions.set(version.versionId, version); });
    await cdp.send("ServiceWorker.enable");
    for (let tries = 0; tries < 20 && ![...versions.values()].some((version) => version.scriptURL === worker.url()); tries++) await new Promise((resolve) => setTimeout(resolve, 50));
    const version = [...versions.values()].find((value) => value.scriptURL === worker.url());
    assert.ok(version, "CDP must identify only this extension worker before stopping it");
    await cdp.send("ServiceWorker.stopWorker", { versionId: version.versionId });
    for (let tries = 0; tries < 40 && versions.get(version.versionId).runningStatus !== "stopped"; tries++) await new Promise((resolve) => setTimeout(resolve, 50));
    assert.equal(versions.get(version.versionId).runningStatus, "stopped", "CDP must confirm the actual worker stopped, not just that stop was requested");
    const nextPage = await context.newPage(); await nextPage.goto("https://www.youtube.com/");
    const newPopup = await context.newPage(); await newPopup.goto(`chrome-extension://${identifier}/popup.html`);
    await nextPage.waitForFunction(() => document.querySelector("[data-yct-badge]")?.textContent.includes("paused"));
    await newPopup.waitForFunction(() => document.querySelector("#status").textContent.includes("Paused") && !document.querySelector("#pause").disabled);
    await popup.waitForFunction(() => document.querySelector("#pause").textContent === "Resume" && !document.querySelector("#pause").disabled);
    assert.equal(await nextPage.locator("[data-yct-category]").count(), 0);
    assert.equal(await page.locator("[data-yct-category]").count(), 0);
    assert.equal(await fs.readFile(nativeEvents, "utf8"), eventsBeforeStop, "paused restart must launch no native companion and dispatch no analysis");
    await runs.waitForFunction(() => document.querySelector("#connection").textContent.includes("Paused"));
    assert.ok(await runs.locator("#recent .attempt").count() >= recordsBeforeRetry, "session results survive worker restart");
    await popup.locator("#pause").click(); await page.waitForSelector('[data-yct-category="valid"]');
    await nextPage.waitForSelector('[data-yct-category="valid"]');
    await cdp.detach();
    await nextPage.reload(); await nextPage.waitForSelector('[data-yct-category="valid"]');
    await nextPage.goto("https://example.test/"); await nextPage.goBack({ waitUntil: "commit" });
    await nextPage.waitForSelector('[data-yct-category="valid"]');
    if (await nextPage.evaluate(() => window.__lastPageShowPersisted)) {
      await nextPage.locator("#home #video-title").evaluate((node) => { node.title = "Valid BFCache restored title"; node.textContent = node.title; });
      await nextPage.waitForSelector('#home[data-yct-category="valid"]');
      await nextPage.goto("https://example.test/"); await nextPage.goBack({ waitUntil: "commit" });
      assert.ok(await nextPage.evaluate(() => window.__lastPageShowPersisted));
      await nextPage.waitForSelector('#home[data-yct-category="valid"]');
      console.log("actual Chromium BFCache: two persisted restorations and recycled title passed");
    } else {
      console.log("actual Chromium BFCache was not used; production persisted-event regression is simulated, not a real BFCache claim");
    }
    await popup.locator("#pause").click(); await runs.waitForFunction(() => document.querySelector("#connection").textContent.includes("Paused"));
    await runs.locator("#clear").click(); await runs.waitForFunction(() => document.querySelector("#recent").textContent.includes("No completed attempts"));
    assert.ok((await runs.locator("#connection").textContent()).includes("Paused"),"clearing activity must not resume or cancel/change pause state");
    console.log(`isolated Chromium extension ${identifier}: native framing, 8 eligible cards, exact four colors, recycling/no-majority, dynamic unavailable, real worker-stop/pause persistence and resume passed`);
    console.log(`TEMP results-page QA: ${qa}`);
  } finally {
    await context?.close();
    await fs.rm(temporary, { recursive: true, force: true });
  }
});
