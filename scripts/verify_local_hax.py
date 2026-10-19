"""Synthetic real-CLI smoke; set HAX_HAX_CLI, optionally --browser Chrome path.

Uses only the application's managed HAX dependency. Installs nothing. All
generated sites, browser profile and evidence live under a fresh temporary root
or a new --output directory. Never points a browser at an existing user profile.
"""

import argparse
import base64
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from local_hax import LocalHaxSites, _environment, _toolchain, build_local_hax_site


BROWSER = r"""
const {createRequire} = require('module');
const req = createRequire(process.argv[1]);
const puppeteer = req('puppeteer-core');
const browserPath = process.argv[2], url = process.argv[3], output = process.argv[4];
const path = require('path');
(async() => {
  const browser = await puppeteer.launch({
    executablePath: browserPath, headless:true,
    userDataDir:path.join(output,'chrome-synthetic-profile'),
    args:['--no-first-run','--disable-background-networking','--disable-extensions']
  });
  const external = new Set();
  try {
    const page = await browser.newPage();
    await page.setViewport({width:1280,height:900});
    page.on('request', request => {
      const parsed = new URL(request.url());
      if (['https:','http:'].includes(parsed.protocol) && parsed.origin !== new URL(url).origin)
        external.add(parsed.origin);
    });
    const deepText = () => {
      function text(node) {
        let value = node.nodeType===Node.TEXT_NODE ? node.textContent : '';
        if (node.shadowRoot) value += text(node.shadowRoot);
        for (const child of node.childNodes || []) value += text(child);
        return value;
      }
      return text(document.body);
    };
    await page.goto(url,{waitUntil:'domcontentloaded',timeout:45000});
    await page.waitForFunction(() => {
      function collect(node) {
        let s=node.nodeType===Node.TEXT_NODE ? node.textContent:'';
        if(node.shadowRoot)s+=collect(node.shadowRoot);
        for(const child of node.childNodes || [])s+=collect(child);
        return s;
      }
      return collect(document.body).includes('Synthetic approved first lesson');
    },{timeout:90000});
    await page.waitForFunction(() => {
      function collect(node) {
        let s=node.nodeType===Node.TEXT_NODE ? node.textContent:'';
        if(node.shadowRoot)s+=collect(node.shadowRoot);
        for(const child of node.childNodes || [])s+=collect(child);
        return s;
      }
      const overlay = document.querySelector('#loading');
      const text = collect(document.body);
      return (!overlay || overlay.hidden) && text.includes('First lesson')
        && text.includes('Second lesson') && !!customElements.get('clean-one');
    },{timeout:90000});
    const first = await page.evaluate(deepText);
    if (!first.includes('Synthetic approved first lesson')) throw Error('First approved page did not render');
    const image = await page.evaluate(() => {
      function find(root) {
        for(const node of root.querySelectorAll('*')) {
          if(node.tagName==='IMG' && node.src.includes('pixel.png'))
            return {src:node.src,loaded:node.complete && node.naturalWidth>0};
          if(node.shadowRoot) {const match=find(node.shadowRoot);if(match)return match;}
        }
      }
      return find(document);
    });
    if (!image || !image.loaded) throw Error('Retained course image did not render');
    await page.screenshot({path:path.join(output,'hax-first-page.png'),fullPage:true});
    const clicked = await page.evaluate(() => {
      function find(root) {
        for(const node of root.querySelectorAll('*')) {
          if(node.tagName==='A' && node.textContent.includes('Continue to second lesson')) {
            node.click();return true;
          }
          if(node.shadowRoot && find(node.shadowRoot))return true;
        }
        return false;
      }
      return find(document);
    });
    if(!clicked)throw Error('Approved cross-page link was not found');
    await page.waitForFunction(() => {
      function collect(node) {
        let s=node.nodeType===Node.TEXT_NODE ? node.textContent:'';
        if(node.shadowRoot)s+=collect(node.shadowRoot);
        for(const child of node.childNodes || [])s+=collect(child);
        return s;
      }
      return collect(document.body).includes('Synthetic approved second lesson');
    },{timeout:45000});
    await new Promise(resolve => setTimeout(resolve,500));
    if(!new URL(page.url()).pathname.includes('lesson-2'))throw Error('HAX slug navigation failed');
    await page.screenshot({path:path.join(output,'hax-second-page.png'),fullPage:true});
    console.log(JSON.stringify({rendered:true,navigation:true,media:true,url:page.url(),
      external_origins:[...external].sort()}));
  } finally {await browser.close();}
})().catch(error=>{console.error(error.message);process.exitCode=1;});
"""


def wait(sites):
    deadline = time.monotonic() + 55
    while time.monotonic() < deadline:
        result = sites.status("synthetic-owner", "synthetic-course")
        if result["phase"] == "ready":
            return result
        if result["phase"] == "failed":
            raise RuntimeError(result["error"])
        time.sleep(.1)
    raise RuntimeError("Runtime did not become ready.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Fresh output directory; never an existing site")
    parser.add_argument("--browser", type=Path, help="Optional Chrome executable for rendered verification")
    args = parser.parse_args()
    if args.output:
        root = args.output.resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
    else:
        root = Path(tempfile.mkdtemp(prefix="hax-real-smoke-")).resolve()
    export = root / "export/files"
    export.mkdir(parents=True)
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jP1sAAAAASUVORK5CYII=")
    (export / "pixel.png").write_bytes(image)
    (export / "pixel.json").write_text('{"filename":"pixel.png"}')
    state = {
        "id": "synthetic-course", "title": "Synthetic HAX verification",
        "revision": 2, "direction_revision": 2, "draft_revision": 3,
        "reviewed_draft_revision": 3, "build_revision": 3,
        "export_dir": str(export.parent),
        "draft": {"items": [
            {"id": "first", "title": "First lesson",
             "html": '<h1>Synthetic approved first lesson</h1><p>Approved synthetic content only.</p>'
                     '<img alt="Synthetic pixel" src="files/pixel.png">'
                     '<p><a href="$IMSCC_ITEM_REFERENCE$/second">Continue to second lesson</a></p>'},
            {"id": "second", "title": "Second lesson",
             "html": '<h1>Synthetic approved second lesson</h1><p>Second approved page.</p>'},
        ]},
    }
    artifact = build_local_hax_site(state, root / "sites/synthetic-course/build-one",
                                    lambda phase, message: print(phase + ": " + message, flush=True))
    sites = LocalHaxSites(root / "sites")
    occupied = socket.socket()
    occupied.bind(("127.0.0.1", 0))
    try:
        initial = sites.start("synthetic-owner", state["id"], "build-one", artifact["directory"])
        assert initial["phase"] == "starting" and initial["url"] is None
        ready = wait(sites)
        print(json.dumps(ready), flush=True)
        assert int(ready["url"].split(":")[-1].rstrip("/")) != occupied.getsockname()[1]
        assert urlopen(ready["url"] + "files/pixel.png", timeout=3).read() == image
        for headers, data in (({"Origin": "https://synthetic-attacker.invalid"}, b"{}"),
                              ({"Host": "synthetic-attacker.invalid"}, None)):
            try:
                urlopen(Request(ready["url"], data=data, headers=headers), timeout=3)
                raise AssertionError("Cross-origin request was not rejected")
            except HTTPError as error:
                assert error.code == 403
        rendered = None
        if args.browser:
            toolchain = _toolchain()
            browser_home = root / "browser-home"
            browser_home.mkdir()
            completed = subprocess.run(
                [toolchain[0], "-e", BROWSER, toolchain[1], str(args.browser), ready["url"], str(root)],
                env=_environment(browser_home), cwd=root, capture_output=True,
                text=True, timeout=180, check=False)
            if completed.returncode:
                raise RuntimeError("Rendered verification failed: " + completed.stderr[-6000:])
            rendered = json.loads(completed.stdout.strip().splitlines()[-1])
            print(json.dumps(rendered), flush=True)
        old_process = sites._entries[("synthetic-owner", state["id"])]["process"]
        sites.stop("synthetic-owner", state["id"])
        assert old_process.poll() is not None
        sites.shutdown()
        # A fresh manager uses the exact saved artifact; no mtime directory discovery.
        sites = LocalHaxSites(root / "sites")
        sites.start("synthetic-owner", state["id"], "build-one", artifact["directory"])
        restarted = wait(sites)
        print(json.dumps({"restart": restarted, "rendered_check": bool(rendered),
                          "evidence_directory": str(root)}), flush=True)
    finally:
        sites.shutdown()
        occupied.close()


if __name__ == "__main__":
    main()
