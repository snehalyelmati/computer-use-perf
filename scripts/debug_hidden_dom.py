"""Debug: inspect the Hidden DOM Challenge (step 4) to find where the code is hidden.

Checks all elements for data-* attributes, aria labels, meta tags, comments,
and hidden text that might contain a 6-digit code.
"""

import asyncio

from playwright.async_api import async_playwright

TARGET_URL = "https://serene-frangipane-7fd25b.netlify.app/"

INSPECT_JS = r"""
(() => {
  const results = {};

  // 1. Check all data-* and aria-* attributes on every element
  const dataAttrs = [];
  for (const el of document.querySelectorAll('*')) {
    for (const attr of el.attributes) {
      if (attr.name.startsWith('data-') || attr.name.startsWith('aria-')) {
        const tag = el.tagName;
        const text = (el.textContent || '').trim().substring(0, 50);
        dataAttrs.push({
          tag, attr: attr.name, value: attr.value,
          text: text.substring(0, 40),
          classes: el.className ? String(el.className).substring(0, 60) : ''
        });
      }
    }
  }
  results.dataAttrs = dataAttrs;

  // 2. Check meta tags
  const metas = [];
  for (const meta of document.querySelectorAll('meta')) {
    metas.push({
      name: meta.getAttribute('name') || meta.getAttribute('property') || '',
      content: meta.getAttribute('content') || '',
      httpEquiv: meta.getAttribute('http-equiv') || ''
    });
  }
  results.metas = metas;

  // 3. Look for the challenge div and inspect its React props/fiber
  const challengeDiv = document.querySelector('.cursor-pointer');
  if (challengeDiv) {
    results.challengeDiv = {
      tag: challengeDiv.tagName,
      text: (challengeDiv.textContent || '').trim().substring(0, 200),
      attributeCount: challengeDiv.attributes.length,
      attributes: {},
      reactProps: null,
      fiberProps: null,
    };
    for (const attr of challengeDiv.attributes) {
      results.challengeDiv.attributes[attr.name] = attr.value;
    }
    // Check React internals
    const keys = Object.keys(challengeDiv);
    for (const key of keys) {
      if (key.startsWith('__reactProps$')) {
        try {
          const props = challengeDiv[key];
          results.challengeDiv.reactProps = {};
          for (const [k, v] of Object.entries(props)) {
            results.challengeDiv.reactProps[k] = typeof v === 'function'
              ? '[function ' + (v.name || 'anon') + ']'
              : JSON.stringify(v).substring(0, 200);
          }
        } catch(e) {}
      }
      if (key.startsWith('__reactFiber$')) {
        try {
          const fiber = challengeDiv[key];
          if (fiber && fiber.memoizedProps) {
            results.challengeDiv.fiberProps = {};
            for (const [k, v] of Object.entries(fiber.memoizedProps)) {
              results.challengeDiv.fiberProps[k] = typeof v === 'function'
                ? '[function ' + (v.name || 'anon') + ']'
                : typeof v === 'object'
                  ? '[object]'
                  : String(v).substring(0, 200);
            }
          }
        } catch(e) {}
      }
    }
  }

  // 4. Also check the parent container of the challenge
  const challengeContainer = document.querySelector('.bg-gray-100.border-gray-400');
  if (challengeContainer) {
    results.challengeContainer = {
      tag: challengeContainer.tagName,
      attributes: {},
      reactProps: null,
      fiberProps: null,
    };
    for (const attr of challengeContainer.attributes) {
      results.challengeContainer.attributes[attr.name] = attr.value;
    }
    const keys = Object.keys(challengeContainer);
    for (const key of keys) {
      if (key.startsWith('__reactProps$')) {
        try {
          const props = challengeContainer[key];
          results.challengeContainer.reactProps = {};
          for (const [k, v] of Object.entries(props)) {
            results.challengeContainer.reactProps[k] = typeof v === 'function'
              ? '[function ' + (v.name || 'anon') + ']'
              : JSON.stringify(v).substring(0, 200);
          }
        } catch(e) {}
      }
      if (key.startsWith('__reactFiber$')) {
        try {
          const fiber = challengeContainer[key];
          if (fiber && fiber.memoizedProps) {
            results.challengeContainer.fiberProps = {};
            for (const [k, v] of Object.entries(fiber.memoizedProps)) {
              results.challengeContainer.fiberProps[k] = typeof v === 'function'
                ? '[function ' + (v.name || 'anon') + ']'
                : typeof v === 'object'
                  ? '[object]'
                  : String(v).substring(0, 200);
            }
          }
        } catch(e) {}
      }
    }
  }

  // 5. Search for any 6-char alphanumeric code in ALL text content
  const body = document.body.textContent;
  const codePattern = /[A-Z0-9]{6}/g;
  const matches = body.match(codePattern) || [];
  results.potentialCodes = [...new Set(matches)].filter(m =>
    !m.match(/^(BUTTON|SHADOW|INLINE|SELECT|OPTION|HIDDEN|SCROLL)$/)
  ).slice(0, 20);

  // 6. Check HTML comments
  const comments = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_COMMENT);
  while (walker.nextNode()) {
    comments.push(walker.currentNode.textContent.trim().substring(0, 100));
  }
  results.comments = comments;

  // 7. Check style attributes and hidden elements that might contain codes
  const hiddenEls = [];
  const selectors = '[style*="display:none"], [style*="display: none"], ' +
    '[style*="visibility:hidden"], [style*="visibility: hidden"], ' +
    '[hidden], .hidden, [aria-hidden="true"]';
  for (const el of document.querySelectorAll(selectors)) {
    const text = (el.textContent || '').trim();
    if (text && text.length < 100) {
      hiddenEls.push({
        tag: el.tagName,
        text,
        style: (el.getAttribute('style') || '').substring(0, 80)
      });
    }
  }
  results.hiddenElements = hiddenEls;

  return results;
})()
"""

# After clicking the challenge div 3 times
AFTER_CLICK_JS = r"""
(() => {
  const results = {};

  // Re-check the challenge area after 3 clicks
  const challengeDiv = document.querySelector('.cursor-pointer');
  if (challengeDiv) {
    results.text = challengeDiv.textContent.trim().substring(0, 500);
    results.attributes = {};
    for (const attr of challengeDiv.attributes) {
      results.attributes[attr.name] = attr.value;
    }
  }

  // Check for any newly revealed elements or codes
  const spans = document.querySelectorAll('span.font-mono, [class*="code"], [class*="reveal"]');
  results.codeSpans = [];
  for (const span of spans) {
    results.codeSpans.push({
      tag: span.tagName,
      text: span.textContent.trim().substring(0, 100),
      classes: span.className ? String(span.className).substring(0, 80) : ''
    });
  }

  // Also check data attributes again
  const dataAttrs = [];
  const parent = challengeDiv || document.body;
  for (const el of [parent, ...parent.querySelectorAll('*')]) {
    for (const attr of el.attributes) {
      if (attr.name.startsWith('data-') && !attr.name.startsWith('data-agent')) {
        dataAttrs.push({ tag: el.tagName, attr: attr.name, value: attr.value });
      }
    }
  }
  results.dataAttrs = dataAttrs;

  // Check for ALL 6-char codes in text now
  const body = document.body.textContent;
  const codePattern = /[A-Z0-9]{6}/g;
  const matches = body.match(codePattern) || [];
  results.potentialCodes = [...new Set(matches)].filter(m =>
    !m.match(/^(BUTTON|SHADOW|INLINE|SELECT|OPTION|HIDDEN|SCROLL)$/)
  ).slice(0, 20);

  return results;
})()
"""


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 720})

        # Navigate to step 4
        await page.goto(TARGET_URL, wait_until="networkidle")
        await page.wait_for_timeout(2000)

        # Click START
        await page.click("button:has-text('START')")
        await page.wait_for_timeout(2000)

        # Get the step1 code and proceed (need to navigate to step 4)
        url = page.url
        version = url.split("version=")[1] if "version=" in url else "1"
        print(f"Version: {version}")

        # Navigate to step4 directly
        await page.goto(
            f"https://serene-frangipane-7fd25b.netlify.app/step4?version={version}",
            wait_until="networkidle",
        )
        await page.wait_for_timeout(3000)

        print(f"\n{'=' * 70}")
        print("STEP 4 - BEFORE clicking challenge div")
        print(f"URL: {page.url}")
        print(f"{'=' * 70}")

        results = await page.evaluate(INSPECT_JS)

        print(f"\n--- Data/Aria attributes on page ({len(results['dataAttrs'])} found) ---")
        for attr in results["dataAttrs"][:30]:
            if not attr["attr"].startswith("data-radix") and not attr["attr"].startswith("data-state"):
                print(f"  <{attr['tag']}> {attr['attr']}=\"{attr['value']}\" | text: {attr['text'][:40]}")

        print(f"\n--- Meta tags ({len(results['metas'])} found) ---")
        for meta in results["metas"]:
            if meta["content"]:
                print(f"  name={meta['name']} content={meta['content'][:60]}")

        print("\n--- Challenge div ---")
        if results.get("challengeDiv"):
            cd = results["challengeDiv"]
            print(f"  Tag: {cd['tag']}, Attrs: {cd['attributes']}")
            print(f"  React props: {cd['reactProps']}")
            print(f"  Fiber props: {cd['fiberProps']}")

        print("\n--- Challenge container ---")
        if results.get("challengeContainer"):
            cc = results["challengeContainer"]
            print(f"  Attrs: {cc['attributes']}")
            print(f"  React props: {cc['reactProps']}")
            print(f"  Fiber props: {cc['fiberProps']}")

        print("\n--- Potential 6-char codes in text ---")
        print(f"  {results['potentialCodes']}")

        print("\n--- HTML comments ---")
        print(f"  {results['comments']}")

        print("\n--- Hidden elements ---")
        for el in results["hiddenElements"]:
            print(f"  <{el['tag']}> text=\"{el['text']}\" style=\"{el['style']}\"")

        # Now click the challenge div 3 times
        print(f"\n{'=' * 70}")
        print("Clicking challenge div (.cursor-pointer) 3 times...")
        print(f"{'=' * 70}")

        for i in range(3):
            await page.click(".cursor-pointer")
            await page.wait_for_timeout(500)
            print(f"  Click {i + 1} done")

        await page.wait_for_timeout(1000)

        after = await page.evaluate(AFTER_CLICK_JS)
        print("\n--- After 3 clicks ---")
        print(f"  Text: {after.get('text', '')[:300]}")
        print(f"  Data attrs: {after.get('dataAttrs', [])}")
        print(f"  Code spans: {after.get('codeSpans', [])}")
        print(f"  Potential codes: {after.get('potentialCodes', [])}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
