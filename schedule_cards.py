"""Deterministic, high-resolution schedule cards.

AI-generated backgrounds may be added later, but timetable text is always
rendered from the college source to avoid hallucinated subjects or times.
"""
from __future__ import annotations
import io
import math
import re
from datetime import datetime
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

WIDTH = 1080
INK = "#F4F7FF"
MUTED = "#A6B9D0"
PANEL = "#152940"
BLUE = "#447BFF"
RED = "#FF4E67"

def font(size: int, bold: bool = False):
    suffix = "-Bold" if bold else ""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans"+suffix+".ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans"+("-Bold" if bold else "-Regular")+".ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()

def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()

def extract_rows(html: str) -> list[list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    rows = []
    for tr in soup.select("tr"):
        cells = [clean(cell.get_text(" ", strip=True)) for cell in tr.find_all(["th", "td"], recursive=False)]
        cells = [x for x in cells if x]
        if cells:
            rows.append(cells)
    if rows:
        return rows
    lines = [clean(line) for line in soup.get_text("\n", strip=True).splitlines()]
    return [[line] for line in lines if line]

def split_lines(draw, value, face, max_width):
    words = value.split()
    lines, current = [], ""
    for word in words:
        candidate = (current+" "+word).strip()
        if draw.textbbox((0,0),candidate,font=face)[2] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            if draw.textbbox((0,0),word,font=face)[2] > max_width:
                # Split very long unbroken tokens without losing characters.
                current = ""
                for char in word:
                    candidate = current+char
                    if current and draw.textbbox((0,0),candidate,font=face)[2] > max_width:
                        lines.append(current)
                        current = char
                    else:
                        current = candidate
            else:
                current = word
    if current:
        lines.append(current)
    return lines or [""]

def render_schedule(name: str, html: str, theme: str = "dark") -> bytes:
    rows = extract_rows(html)
    if not rows:
        rows = [["Расписание пока не опубликовано"]]
    dark = theme != "light"
    bg = "#071626" if dark else "#F0F4FA"
    panel = PANEL if dark else "#FFFFFF"
    ink = INK if dark else "#15243B"
    muted = MUTED if dark else "#5C6C81"
    title_face, label_face, row_face = font(54,True), font(24,True), font(28)
    temp = Image.new("RGB",(WIDTH,200),"white")
    measure = ImageDraw.Draw(temp)
    title_lines = split_lines(measure,clean(name),title_face,WIDTH-130)[:3]
    cards = []
    for cells in rows[:100]:
        line_text = "   •   ".join(cells)
        lines = split_lines(measure,line_text,row_face,WIDTH-170)
        cards.append(lines)
    # Telegram photo dimensions must stay within platform limits.
    # If the source is long, split into multiple PNG pages instead of clipping.
    # This renderer outputs a single page; use a compact layout for long tables.
    if len(cards)>55:
        cards=cards[:55]
        cards.append(["Продолжение расписания смотри на rktm.info"])
    card_heights = [max(74,32+len(lines)*39) for lines in cards]
    header_height = 240+len(title_lines)*70
    height = header_height+sum(card_heights)+125+max(0,len(cards)-1)*12
    im = Image.new("RGB",(WIDTH,height),bg)
    d = ImageDraw.Draw(im)
    # Subtle premium grid and layered gradients
    for y in range(height):
        factor = y/max(1,height-1)
        if dark:
            color=(int(7+5*factor),int(22+9*factor),int(38+14*factor))
        else:
            color=(int(240-5*factor),int(244-4*factor),int(250-4*factor))
        d.line((0,y,WIDTH,y),fill=color)
    for x in range(0,WIDTH,72):
        d.line((x,0,x,height),fill="#12283B" if dark else "#E5EAF2",width=1)
    d.rounded_rectangle((52,55,260,107),radius=24,fill=BLUE)
    d.text((80,64),"РКТМ  •  NEXT",font=font(23,True),fill="#FFFFFF")
    d.rounded_rectangle((WIDTH-250,55,WIDTH-52,107),radius=24,fill=RED)
    d.text((WIDTH-220,65),"РАСПИСАНИЕ",font=font(19,True),fill="#FFFFFF")
    y=145
    for line in title_lines:
        d.text((62,y),line,font=title_face,fill=ink)
        y+=70
    d.text((64,y+12),"АКТУАЛЬНЫЕ ДАННЫЕ С САЙТА КОЛЛЕДЖА",font=label_face,fill=muted)
    y=header_height
    for i,(lines,h) in enumerate(zip(cards,card_heights)):
        d.rounded_rectangle((52,y,WIDTH-52,y+h),radius=20,fill=panel)
        d.rounded_rectangle((52,y+13,59,y+h-13),radius=3,fill=RED if i%2 else BLUE)
        ty=y+17
        for line in lines:
            d.text((85,ty),line,font=row_face,fill=ink)
            ty+=39
        y+=h+12
    d.line((62,height-84,WIDTH-62,height-84),fill=muted,width=1)
    d.text((64,height-65),"ИСТОЧНИК: RКТМ.INFO",font=font(20,True),fill=muted)
    d.text((WIDTH-250,height-65),"RКТМ  /  2026",font=font(20),fill=muted)
    out=io.BytesIO()
    im.save(out,format="PNG",optimize=True)
    return out.getvalue()
