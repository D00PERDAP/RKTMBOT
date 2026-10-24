"""Render schedule as styled Telegram-ready PNG pages using headless Chromium."""
from __future__ import annotations
import asyncio
from html import escape
from bs4 import BeautifulSoup

CSS = """
*{box-sizing:border-box}body{margin:0;background:#07111f;color:#eef5ff;font-family:Arial,'DejaVu Sans',sans-serif}
.page{width:1080px;padding:62px 58px 74px;background:radial-gradient(circle at 92% 0%,#213b66 0,transparent 40%),linear-gradient(160deg,#081525,#0e2034)}
.brand{display:flex;justify-content:space-between;align-items:center;gap:12px}
.pill{background:#2d65e9;color:white;border-radius:24px;padding:14px 23px;font-size:21px;font-weight:800;letter-spacing:1px}
.pill.red{background:#d94362}
h1{font-size:58px;line-height:1.16;overflow-wrap:anywhere;margin:46px 0 12px;font-weight:850}
.subtitle{font-size:23px;color:#a9bfd8;margin:0 0 34px}
.card{background:#152a43;border:1px solid #284361;border-radius:21px;margin:12px 0;padding:22px 25px;display:flex;gap:21px;align-items:flex-start}
.num{background:#2c5fe0;border-radius:13px;min-width:52px;text-align:center;padding:12px 0;font-size:24px;font-weight:800}
.content{flex:1;min-width:0;font-size:27px;line-height:1.4;overflow-wrap:anywhere;white-space:pre-wrap}
.content span{display:block}.content span+span{color:#c0d0e4;margin-top:5px}
.footer{display:flex;justify-content:space-between;border-top:1px solid #30445c;margin-top:38px;padding-top:24px;font-size:19px;color:#9fb4cd}
"""
def extract_rows(html: str):
    soup=BeautifulSoup(html,"html.parser")
    for node in soup(["script","style","nav","footer"]):
        node.decompose()
    rows=[]
    for tr in soup.select("tr"):
        cells=[" ".join(x.split()) for x in (c.get_text(" ",strip=True) for c in tr.find_all(["td","th"],recursive=False))]
        cells=[x for x in cells if x]
        if cells:
            rows.append(cells)
    if not rows:
        lines=[" ".join(x.split()) for x in soup.get_text("\n",strip=True).splitlines()]
        rows=[[line] for line in lines if line]
    return rows or [["Расписание пока не опубликовано"]]

def html_page(name,rows,page,total):
    cards=[]
    for idx,cells in rows:
        fields="".join("<span>"+escape(cell)+"</span>" for cell in cells)
        cards.append(f'<article class="card"><div class="num">{idx}</div><div class="content">{fields}</div></article>')
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><style>{CSS}</style></head>
<body><main class="page"><div class="brand"><div class="pill">РКТМ / SCHEDULE</div><div class="pill red">РАСПИСАНИЕ</div></div>
<h1>{escape(name)}</h1><p class="subtitle">Актуальные данные с сайта колледжа</p>
{''.join(cards)}<footer class="footer"><span>Источник: rktm.info</span><span>Страница {page} / {total}</span></footer>
</main></body></html>"""

async def render_schedule_pages(name: str, html: str) -> list[bytes]:
    from playwright.async_api import async_playwright
    rows=extract_rows(html)
    # Limit card height by content length; never silently truncate the timetable.
    batches=[]
    batch=[]
    budget=0
    for idx,cells in enumerate(rows,1):
        cost=max(1,sum(len(x) for x in cells)//70+1)
        if batch and (len(batch)>=12 or budget+cost>22):
            batches.append(batch)
            batch=[]
            budget=0
        batch.append((idx,cells))
        budget+=cost
    if batch:
        batches.append(batch)
    result=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,args=["--no-sandbox"])
        try:
            page=await browser.new_page(viewport={"width":1080,"height":800},device_scale_factor=1)
            for n,batch in enumerate(batches,1):
                await page.set_content(html_page(name,batch,n,len(batches)),wait_until="load")
                image=await page.locator(".page").screenshot(type="png",animations="disabled",timeout=20000)
                result.append(image)
        finally:
            await browser.close()
    return result
