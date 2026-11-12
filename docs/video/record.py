"""Drive a local Leash dashboard through the demo and capture crisp frames.

Usage: python record.py <out_dir> <base_url> <token> <chromium_path>
Writes <out_dir>/video/*.webm (1920x1080) and marks.json (seconds from video start).
"""
import asyncio, base64, json, sys, time
from pathlib import Path
from playwright.async_api import async_playwright

OUT, BASE, TOKEN, CHROME = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
OUT.mkdir(parents=True, exist_ok=True)
marks = []
T0 = [0.0]
last_agent = 0.0

CURSOR_JS = """
addEventListener('DOMContentLoaded', () => {
  document.documentElement.style.zoom = '1.5';
  const st = document.createElement('style');
  st.textContent = '#askOut,#holds,#ledgerGrid,#ledgerSearch,#limits,#askForm,#reasonSummary,#limitHistory{scroll-margin-top:30px}';
  document.head.appendChild(st);
  const c = document.createElement('div');
  c.id = '__cursor';
  c.style.cssText = 'position:fixed;left:-50px;top:-50px;width:22px;height:22px;border-radius:50%;'
    + 'background:rgba(255,196,0,.55);border:2px solid #0b2a6f;z-index:99999;pointer-events:none;'
    + 'transform:translate(-50%,-50%);transition:width .12s,height .12s';
  document.body.appendChild(c);
  addEventListener('mousemove', e => { c.style.left = (e.clientX / 1.5) + 'px'; c.style.top = (e.clientY / 1.5) + 'px'; }, true);
  addEventListener('mousedown', () => { c.style.width = c.style.height = '14px'; }, true);
  addEventListener('mouseup', () => { c.style.width = c.style.height = '22px'; }, true);
});
"""


def mark(name):
    marks.append({"name": name, "t": round(time.time() - T0[0], 2)}); print(f"{time.strftime('%X')} {name}", flush=True)


async def main():
    global last_agent
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=CHROME)
        ctx = await b.new_context(viewport={"width": 1920, "height": 1080}, accept_downloads=True,
                                  record_video_dir=str(OUT / "video"),
                                  record_video_size={"width": 1920, "height": 1080})
        await ctx.add_init_script(CURSOR_JS)
        pg = await ctx.new_page()
        T0[0] = time.time()

        mouse = {"x": 640, "y": 400}

        async def move_to(sel, dx=0, dy=0):
            el = pg.locator(sel).first
            await el.scroll_into_view_if_needed()
            box = await el.bounding_box()
            x, y = box["x"] + box["width"] / 2 + dx, box["y"] + box["height"] / 2 + dy
            await pg.mouse.move(x, y, steps=20); mouse.update(x=x, y=y)
            await pg.wait_for_timeout(250)

        async def click(sel):
            await move_to(sel)
            await pg.mouse.down(); await pg.wait_for_timeout(90); await pg.mouse.up()
            await pg.locator(sel).first.click(trial=True) if False else None

        async def real_click(sel):
            await move_to(sel)
            await pg.locator(sel).first.click()

        async def scroll_to(sel, block="start", offset=0):
            await pg.evaluate("([s, b]) => document.querySelector(s).scrollIntoView({behavior: 'smooth', block: b})", [sel, block])
            await pg.wait_for_timeout(1100)

        async def wait_agent():
            await pg.wait_for_timeout(600)
            await pg.wait_for_function("!document.getElementById('askButton').disabled", timeout=240000)
            await pg.wait_for_timeout(1500)
            n = len(marks)
            (OUT / f"askout_{n:02d}.txt").write_text(await pg.inner_text("#askOut"))

        async def agent_gap():
            global last_agent
            gap = 24 - (time.time() - last_agent)
            if gap > 0:
                await pg.wait_for_timeout(gap * 1000)
            last_agent = time.time()

        await pg.goto(BASE)
        await pg.fill("#tokenInput", TOKEN)
        await pg.click("text=Open dashboard")
        await pg.wait_for_timeout(2500)
        await pg.mouse.move(640, 380)
        await pg.wait_for_timeout(1000)

        # 1. The problem
        mark("problem")
        await move_to("#scale", dx=-200); await pg.wait_for_timeout(1500)
        await move_to("#scale", dx=120); await pg.wait_for_timeout(1500)
        await move_to(".budget-bar"); await pg.wait_for_timeout(1500)
        await move_to("#hijackButton", dy=40); await pg.wait_for_timeout(1200)
        mark("problem_end")

        # 2. Small gift
        await agent_gap()
        await scroll_to("#askForm", offset=120)
        mark("gift_click")
        await real_click("button[data-example*=birthday]")
        await pg.wait_for_timeout(1500)
        mark("gift_wait")
        await wait_agent()
        mark("gift_done")
        await scroll_to("#askOut", offset=140)
        await move_to("#askOut .stamp"); await pg.wait_for_timeout(2500)
        await scroll_to("#ledgerGrid", offset=80)
        await move_to("#ledgerGrid .ag-row[row-index='0']"); await pg.wait_for_timeout(3500)
        mark("gift_end")

        # 3. Gift hamper -> held -> approve
        await agent_gap()
        await scroll_to("#askForm", offset=120)
        mark("hamper_click")
        await real_click("button[data-example*=hamper]")
        await pg.wait_for_timeout(1500)
        mark("hamper_wait")
        await wait_agent()
        mark("hamper_done")
        await scroll_to("#askOut", offset=140)
        await move_to("#askOut .stamp"); await pg.wait_for_timeout(2000)
        await scroll_to("#holds", offset=60)
        held = await pg.locator("#holds button.sign").count()
        mark("hamper_holds")
        if held:
            await move_to("#holds .card, #holds > *"); await pg.wait_for_timeout(2500)
            mark("hamper_approve")
            await real_click("#holds button.sign")
            await pg.wait_for_timeout(3000)
            await scroll_to("#ledgerGrid", offset=80)
            await move_to("#ledgerGrid .ag-row[row-index='0']"); await pg.wait_for_timeout(3500)
        mark("hamper_end")

        # 4. Hijacked agent (no AI)
        await scroll_to("#askForm", offset=120)
        mark("hijack_click")
        await real_click("#hijackButton")
        await wait_agent()
        mark("hijack_done")
        await scroll_to("#askOut", offset=140)
        await move_to("#askOut .hidden-text"); await pg.wait_for_timeout(4000)
        await move_to("#askOut .stamp"); await pg.wait_for_timeout(2500)
        await move_to("#askOut .why"); await pg.wait_for_timeout(4000)
        await move_to("#askOut .answer"); await pg.wait_for_timeout(2500)
        mark("hijack_end")

        # 5. Ledger and limits
        await scroll_to("#ledgerSearch", offset=120)
        mark("ledger")
        await real_click("#byReason")
        await pg.wait_for_timeout(1200)
        await move_to("#reasonSummary"); await pg.wait_for_timeout(3500)
        mark("csv")
        async with pg.expect_download() as dl:
            await real_click("#exportCsv")
        d = await dl.value
        await d.save_as(OUT / d.suggested_filename)
        mark("csv_saved:" + d.suggested_filename)
        await pg.wait_for_timeout(2000)
        mark("limits")
        await scroll_to("#limits", offset=60)
        await real_click("#limits > summary")
        await pg.wait_for_timeout(1200)
        await move_to("#limitForm small", dy=-10); await pg.wait_for_timeout(2500)
        await scroll_to("#limitHistory", block="center")
        await move_to("#limitHistory"); await pg.wait_for_timeout(3000)
        await scroll_to("#limits", offset=60)
        await real_click("#limits > summary")
        await pg.wait_for_timeout(1200)
        mark("limits_end")

        # 6. Over the cap
        await agent_gap()
        await scroll_to("#askForm", offset=120)
        mark("cap_click")
        await real_click("button[data-example*=KitchenAid]")
        await pg.wait_for_timeout(1500)
        mark("cap_wait")
        await wait_agent()
        mark("cap_done")
        await scroll_to("#askOut", offset=140)
        for i in range(await pg.locator("#askOut .stamp").count()):
            await move_to(f"#askOut .step:has(.stamp) >> nth={i}"); await pg.wait_for_timeout(3000)
        await move_to("#askOut .answer"); await pg.wait_for_timeout(2500)
        held = await pg.locator("#holds button.refuse").count()
        mark(f"cap_holds:{held}")
        if held:
            await scroll_to("#holds", offset=60)
            await move_to("#holds > *"); await pg.wait_for_timeout(3000)
            mark("cap_deny")
            await real_click("#holds button.refuse")
            await pg.wait_for_timeout(3000)
        mark("cap_end")

        # 7. Close
        await pg.evaluate("scrollTo({top: 0, behavior: 'smooth'})")
        await pg.wait_for_timeout(1000)
        mark("close")
        await pg.mouse.move(1100, 60, steps=20)
        await pg.wait_for_timeout(6000)
        mark("end")
        (OUT / "askout_final.txt").write_text(await pg.inner_text("body"))
        await pg.wait_for_timeout(500)
        await ctx.close(); await b.close()
    (OUT / "marks.json").write_text(json.dumps(marks, indent=1))


asyncio.run(main())
