"""Render schedule as styled Telegram-ready PNG pages using headless Chromium."""
from __future__ import annotations
import asyncio
from html import escape
from bs4 import BeautifulSoup

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#fff;color:#202124;font-family:Arial,'DejaVu Sans',sans-serif}
.page{width:720px;padding:30px 30px 34px;background:#fff}
.brand{display:flex;justify-content:space-between;align-items:center;gap:10px}
.pill{background:#b71c2b;color:#fff;border-radius:10px;padding:9px 14px;font-size:14px;font-weight:800}
.pill.red{background:#fce8eb;color:#a31627}
h1{font-size:33px;line-height:1.2;overflow-wrap:anywhere;margin:26px 0 8px;color:#202124}
.subtitle{font-size:15px;color:#777;margin:0 0 20px}
.card{background:#fff;border:1px solid #e7e7e7;border-left:7px solid #c51f35;border-radius:12px;margin:9px 0;padding:14px 16px;display:flex;gap:14px;align-items:flex-start}
.num{background:#c51f35;color:white;border-radius:9px;min-width:36px;text-align:center;padding:8px 0;font-size:16px;font-weight:800}
.content{flex:1;min-width:0;font-size:18px;line-height:1.36;overflow-wrap:anywhere;white-space:pre-wrap}
.content span{display:block}.content span+span{color:#52565b;margin-top:4px}
.footer{display:flex;justify-content:space-between;border-top:1px solid #ededed;margin-top:24px;padding-top:16px;font-size:13px;color:#777}
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
    if match and match.group(3) and len(value)<45:
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
    """Separate only explicitly marked days, preserving rowspans and column context.

    Never carry a weekday from one table to another and never guess a day from
    lesson numbers or times. Incomplete parsing is signalled by None.
    """
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
        # Horizontal layout: multiple weekday names in the same header row.
        header=None
        for index,row in enumerate(rows[:12]):
            labels=[day_number(v) if len(v)<=24 else None for v in row]
            found={v for v in labels if v is not None}
            if len(found)>=2 and (header is None or len(found)>header[0]):
                header=(len(found),index,labels)
        if header:
            recognized=True
            _,index,labels=header
            for row in rows[index+1:]:
                shared=[v for j,v in enumerate(row) if j>=len(labels) or labels[j] is None]
                for col,day in enumerate(labels):
                    if day is not None and col<len(row):
                        lesson=row[col].strip()
                        if lesson and day_number(lesson) is None:
                            result[DAYS[day]].append([v for v in shared if v]+[lesson])
            continue
        # Vertical layout: day in first column (possibly merged down) or
        # a standalone row. Rows before the first day are not lessons.
        current=None
        previous_marker=None
        for row in rows:
            first=row[0].strip() if row else ""
            marker=day_number(first) if len(first)<=32 else None
            if marker is not None:
                recognized=True
                current=marker
                previous_marker=first
                rest=row[1:]
                if rest and any(v.strip() for v in rest):
                    result[DAYS[current]].append([v for v in rest if v.strip()])
                continue
            # Rows with another explicit day elsewhere: section header only.
            explicit=[day_number(v) for v in row if len(v)<=24]
            if any(v is not None for v in explicit):
                marker=next(v for v in explicit if v is not None)
                recognized=True
                current=marker
                rest=[v for v in row if day_number(v) is None and v.strip()]
                if rest:
                    result[DAYS[current]].append(rest)
                continue
            if current is not None and any(v.strip() for v in row):
                result[DAYS[current]].append([v for v in row if v.strip()])
    if not recognized:
        current=None
        for tag in soup.find_all(["h1","h2","h3","h4","p","li"]):
            if tag.find_parent("table"):
                continue
            value=" ".join(tag.get_text(" ",strip=True).split())
            marker=day_number(value) if len(value)<=32 else None
            if marker is not None:
                current=marker
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
    rows=grouped[DAYS[day_index]]
    if not rows:
        return None
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
