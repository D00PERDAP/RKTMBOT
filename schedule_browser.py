"""Render schedule as styled Telegram-ready PNG pages using headless Chromium."""
from __future__ import annotations
import asyncio
from html import escape
from bs4 import BeautifulSoup

CSS = """
*{box-sizing:border-box}body{margin:0;background:#07111f;color:#eef5ff;font-family:Arial,'DejaVu Sans',sans-serif}
.page{width:760px;padding:36px 34px 42px;background:radial-gradient(circle at 92% 0%,#213b66 0,transparent 40%),linear-gradient(160deg,#081525,#0e2034)}
.brand{display:flex;justify-content:space-between;align-items:center;gap:12px}
.pill{background:#2d65e9;color:white;border-radius:24px;padding:10px 16px;font-size:15px;font-weight:800;letter-spacing:1px}
.pill.red{background:#d94362}
h1{font-size:38px;line-height:1.16;overflow-wrap:anywhere;margin:28px 0 10px;font-weight:850}
.subtitle{font-size:17px;color:#a9bfd8;margin:0 0 22px}
.card{background:#152a43;border:1px solid #284361;border-radius:17px;margin:10px 0;padding:15px 18px;display:flex;gap:21px;align-items:flex-start}
.num{background:#2c5fe0;border-radius:13px;min-width:42px;text-align:center;padding:9px 0;font-size:18px;font-weight:800}
.content{flex:1;min-width:0;font-size:19px;line-height:1.35;overflow-wrap:anywhere;white-space:pre-wrap}
.content span{display:block}.content span+span{color:#c0d0e4;margin-top:5px}
.footer{display:flex;justify-content:space-between;border-top:1px solid #30445c;margin-top:24px;padding-top:16px;font-size:14px;color:#9fb4cd}
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
        if batch and (len(batch)>=5 or budget+cost>9):
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
            page=await browser.new_page(viewport={"width":760,"height":800},device_scale_factor=1)
            for n,batch in enumerate(batches,1):
                await page.set_content(html_page(name,batch,n,len(batches)),wait_until="load")
                image=await page.locator(".page").screenshot(type="png",animations="disabled",timeout=20000)
                result.append(image)
        finally:
            await browser.close()
    return result

DAYS = ("Понедельник","Вторник","Среда","Четверг","Пятница","Суббота")
DAY_SHORT = ("Пн","Вт","Ср","Чт","Пт","Сб")
import re
from datetime import datetime
DAY_PATTERN = re.compile(r"(?<![а-яё])(?:понедельник|вторник|среда|четверг|пятница|суббота|пн\.?|вт\.?|ср\.?|чт\.?|пт\.?|сб\.?)(?![а-яё])",re.I)
DATE_PATTERN = re.compile(r"(?<!\d)(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?(?!\d)")
DAY_NAMES = {"понедельник":0,"пн":0,"вторник":1,"вт":1,"среда":2,"ср":2,"четверг":3,"чт":3,"пятница":4,"пт":4,"суббота":5,"сб":5}

def day_number(value):
    value=" ".join(value.lower().split())
    match=DAY_PATTERN.search(value)
    if match:
        return DAY_NAMES[match.group().rstrip(".")]
    match=DATE_PATTERN.search(value)
    if match and len(value)<45:
        try:
            year=int(match.group(3) or datetime.now().year)
            if year<100:
                year+=2000
            day=datetime(year,int(match.group(2)),int(match.group(1))).weekday()
            return day if day<6 else None
        except ValueError:
            pass
    return None

def table_rows(table):
    pending={}
    rows=[]
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue
        cells=tr.find_all(["td","th"],recursive=False)
        if not cells:
            continue
        row=[]
        col=0
        def fill():
            nonlocal col
            while col in pending:
                value,remaining=pending[col]
                row.append(value)
                if remaining<=1:
                    del pending[col]
                else:
                    pending[col]=(value,remaining-1)
                col+=1
        for cell in cells:
            fill()
            value=" ".join(cell.get_text(" ",strip=True).split())
            span=min(int(cell.get("colspan",1)),40)
            height=min(int(cell.get("rowspan",1)),300)
            for _ in range(span):
                row.append(value)
                if height>1:
                    pending[col]=(value,height-1)
                col+=1
        fill()
        rows.append(row)
    return rows

def split_by_days(html: str):
    """Recognize horizontal weekday columns, vertical sections, rowspans and dated headings."""
    soup=BeautifulSoup(html,"html.parser")
    for tag in soup(["script","style","nav","footer"]):
        tag.decompose()
    result={day:[] for day in DAYS}
    recognized=False
    for table in soup.find_all("table"):
        if table.find_parent("table"):
            continue
        rows=table_rows(table)
        if not rows:
            continue
        header=None
        for index,row in enumerate(rows[:8]):
            labels=[day_number(value) if len(value)<45 else None for value in row]
            unique={x for x in labels if x is not None}
            if len(unique)>=2 and (header is None or len(unique)>header[0]):
                header=(len(unique),index,labels)
        if header:
            recognized=True
            _,index,labels=header
            for row in rows[index+1:]:
                context=[value for col,value in enumerate(row) if col>=len(labels) or labels[col] is None]
                for col,day in enumerate(labels):
                    if day is not None and col<len(row) and row[col].strip():
                        result[DAYS[day]].append(context+[row[col]])
            continue
        current=None
        for row in rows:
            matches=[day_number(value) if len(value)<45 else None for value in row]
            positions=[i for i,day in enumerate(matches) if day is not None]
            if positions and (positions[0]==0 or len(positions)==1):
                current=matches[positions[0]]
                recognized=True
                rest=[value for i,value in enumerate(row) if i not in positions and value]
                if rest:
                    result[DAYS[current]].append(rest)
            elif current is not None and any(row):
                result[DAYS[current]].append([value for value in row if value])
    if not recognized:
        current=None
        for tag in soup.find_all(["h1","h2","h3","h4","p","li"]):
            if tag.find_parent("table"):
                continue
            value=" ".join(tag.get_text(" ",strip=True).split())
            day=day_number(value) if len(value)<45 else None
            if day is not None:
                current=day
                recognized=True
            elif current is not None and value:
                result[DAYS[current]].append([value])
    return result if recognized else None

async def render_day_pages(name: str, html: str, day_index: int):
    if not 0<=day_index<len(DAYS):
        raise ValueError("Invalid weekday")
    grouped=split_by_days(html)
    if grouped is None:
        return None
    rows=grouped[DAYS[day_index]] or [["На этот день пары не указаны"]]
    return await render_rows_pages(name+" · "+DAYS[day_index], rows)

async def render_rows_pages(name: str, rows: list[list[str]]) -> list[bytes]:
    from playwright.async_api import async_playwright
    batches=[]
    batch=[]
    budget=0
    for idx,cells in enumerate(rows,1):
        cost=max(1,sum(len(x) for x in cells)//70+1)
        if batch and (len(batch)>=5 or budget+cost>9):
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
            page=await browser.new_page(viewport={"width":760,"height":800},device_scale_factor=1)
            for n,batch in enumerate(batches,1):
                await page.set_content(html_page(name,batch,n,len(batches)),wait_until="load")
                result.append(await page.locator(".page").screenshot(type="png",animations="disabled",timeout=20000))
        finally:
            await browser.close()
    return result
