"""Shared, deterministic daily PNG cards for Telegram and MAX (no browser)."""
import asyncio
from functools import lru_cache
from html import unescape
import io
import os
from pathlib import Path
import re

from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

WIDTH = 1080
MAX_HEIGHT = 2000
_RENDER_LIMIT = asyncio.Semaphore(2)


def plain(value):
    return unescape(BeautifulSoup(value, 'html.parser').get_text('', strip=False)).strip()


def image_text(value):
    # Native emoji glyphs are not available in standard Linux fonts.
    return re.sub(r'[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0E\uFE0F\u200D\u200B]', '', value).strip()


@lru_cache(maxsize=16)
def face(size, bold=False):
    suffix = '-Bold' if bold else ''
    paths = [
        '/usr/share/fonts/truetype/dejavu/DejaVuSans' + suffix + '.ttf',
        '/usr/share/fonts/truetype/liberation2/LiberationSans' + ('-Bold' if bold else '-Regular') + '.ttf',
        str(Path(os.getenv('WINDIR', 'C:/Windows')) / 'Fonts' / ('arialbd.ttf' if bold else 'arial.ttf')),
    ]
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    raise RuntimeError('Install fonts-dejavu-core to render Cyrillic schedule cards')


def wrap(draw, text, font, width):
    result = []
    for paragraph in text.splitlines() or ['']:
        line = ''
        for word in paragraph.split():
            candidate = (line + ' ' + word).strip()
            if draw.textlength(candidate, font=font) <= width:
                line = candidate
                continue
            if line:
                result.append(line)
            line = ''
            # An unusually long subject/code must not escape the card boundary.
            for char in word:
                if line and draw.textlength(line + char, font=font) > width:
                    result.append(line)
                    line = ''
                line += char
        result.append(line)
    return result


def content_blocks(parts, changes=''):
    blocks = []
    heading = ''
    dates = list(dict.fromkeys(line.strip() for part in parts for line in plain(part).splitlines() if line.strip().startswith('📆')))
    for part in parts:
        for index, raw in enumerate(part.split('\n\n')):
            text = plain(raw)
            if not text:
                continue
            lines = text.splitlines()
            if index == 0 and lines[0].startswith('📅'):
                heading = heading or image_text(lines.pop(0))
            if len(dates) == 1:
                lines = [line for line in lines if line.strip() != dates[0]]
            lines = [image_text(line) for line in lines if image_text(line)]
            if lines:
                blocks.append(('schedule', '\n'.join(lines)))
    if len(dates) == 1:
        heading += '\n' + image_text(dates[0])
    if changes:
        for block in changes.split('\n\n'):
            if block.strip():
                blocks.append(('changes', image_text(block)))
    return heading, blocks or [('schedule', 'Расписание на этот день пока не опубликовано.')]


def header_layout(title, heading, draw):
    title = image_text(title).strip(' ,')
    title_lines = wrap(draw, title, face(40, True), 960)
    # The full name remains in the original heading if unusually long.
    if len(title_lines) > 3:
        title_lines = ['Расписание на день']
    elif heading:
        first, *rest = heading.splitlines()
        if first.startswith(title):
            first = first[len(title):].strip(' ,·')
        heading = '\n'.join([first] + rest)
    metadata = wrap(draw, heading, face(24), 960) if heading else []
    height = 110 + len(title_lines)*50 + len(metadata)*29
    return title_lines, metadata, height


def combine_changes(title, heading, blocks):
    """Merge only one unambiguous group/lesson change, retaining source content."""
    normalize = lambda value: re.sub(r"\s+", "", value).casefold()
    candidates = {}
    base_counts = {}
    for index,(kind,text) in enumerate(blocks):
        if kind=="schedule":
            number = re.match(r"(\d+)-й урок",text)
            if number:
                n = int(number[1])
                base_counts[n] = base_counts.get(n,0)+1
        else:
            header,*lines = text.splitlines()
            match = re.fullmatch(r"(.+?)\s*·\s*Урок\s+(\d+)",header)
            if match and normalize(match[1])==normalize(title.rstrip(' ,')):
                fields = {key.strip():value.strip() for line in lines if ': ' in line for key,value in [line.split(': ',1)]}
                if len(fields)==len(lines) and set(fields).issubset({'Было','Стало','Преподаватель','Кабинет'}) and fields.get('Было') and fields.get('Стало'):
                    candidates.setdefault(int(match[2]),[]).append((index,fields))
    used = set()
    result = []
    for kind,text in blocks:
        lines = text.splitlines()
        number = re.match(r"(\d+)-й урок",lines[0]) if kind=='schedule' else None
        options = candidates.get(int(number[1]),[]) if number else []
        source = '\n'.join(lines[1:])
        if number and base_counts[int(number[1])]==1 and len(options)==1 and normalize(source).startswith(normalize(options[0][1]['Было'])):
            index,fields = options[0]
            merged = [lines[0],'Было: '+source,'Стало: '+fields['Стало']]
            teacher = fields.get('Преподаватель')
            if teacher and normalize(teacher) not in normalize(fields['Стало']):
                merged.append('Преподаватель: '+teacher)
            if fields.get('Кабинет'):
                merged.append('Кабинет: '+fields['Кабинет'])
            result.append(('changes','\n'.join(merged)))
            used.add(index)
        else:
            result.append((kind,text))
    if not used:
        return heading,blocks
    final = []
    for index,(kind,text) in enumerate(result):
        if index in used:
            continue
        if kind=='changes' and text.startswith('Замены на '):
            heading += '\n'+text
        else:
            final.append((kind,text))
    return heading,final


def _arrange(parts, changes='', title=''):
    draw = ImageDraw.Draw(Image.new('RGB', (WIDTH, 1)))
    heading, blocks = content_blocks(parts, changes)
    heading, blocks = combine_changes(title,heading,blocks)
    _, _, header = header_layout(title, heading, draw)
    available = MAX_HEIGHT - header - 70
    if available < 600:
        raise ValueError('Schedule title is too long for a photo; use text')
    alternatives = []
    # Prefer one readable sheet, with a firm minimum type size. Larger days
    # keep the comfortable type size and are shown in a one-message carousel.
    for size, line_height, padding, gap in ((30,38,24,8),(28,35,20,6),(26,33,16,6)):
        cards = []
        maximum = min(36, (available-padding-gap)//line_height-1)
        for kind,text in blocks:
            lines = wrap(draw,text,face(size),916)
            for offset in range(0,len(lines),maximum):
                segment = lines[offset:offset+maximum]
                if offset:
                    segment = ['Продолжение'] + segment
                cards.append((kind,segment))
        pages,page,used = [],[],0
        for card in cards:
            height = padding + line_height*len(card[1]) + gap
            if page and used+height > available:
                pages.append(page)
                page,used = [],0
            page.append(card)
            used += height
        if page:
            pages.append(page)
        result = heading,pages,(size,line_height,padding,gap)
        if len(pages)==1:
            return result
        alternatives.append(result)
    return alternatives[0]


def layout_cards(parts, changes='', title=''):
    heading,pages,_ = _arrange(parts,changes,title)
    return heading,pages


@lru_cache(maxsize=12)
def render_daily_pages(title, parts, changes=''):
    heading,pages,style = _arrange(parts,changes,title)
    size,line_height,padding,gap = style
    measure = ImageDraw.Draw(Image.new('RGB',(WIDTH,1)))
    title_lines,metadata,header = header_layout(title,heading,measure)
    results = []
    for number,cards in enumerate(pages,1):
        height = header + sum(padding+line_height*len(lines)+gap for _,lines in cards)+70
        if height > MAX_HEIGHT:
            raise ValueError('Schedule page exceeds photo dimensions')
        image = Image.new('RGB',(WIDTH,height),'#F3F5F9')
        draw = ImageDraw.Draw(image)
        draw.rectangle((0,0,WIDTH,10),fill='#BE2440')
        draw.rounded_rectangle((40,28,177,69),radius=10,fill='#BE2440')
        draw.text((56,33),'РКТМ',font=face(23,True),fill='white')
        draw.text((200,36),'РАСПИСАНИЕ',font=face(20,True),fill='#667085')
        y = 91
        for line in title_lines:
            draw.text((42,y),line,font=face(40,True),fill='#182230')
            y += 50
        for line in metadata:
            draw.text((42,y+2),line,font=face(24),fill='#667085')
            y += 29
        y = header
        for kind,lines in cards:
            h = padding+line_height*len(lines)
            accent = '#BE2440' if kind=='changes' else '#3661A6'
            draw.rounded_rectangle((40,y,WIDTH-40,y+h),radius=10,fill='#FFF2F4' if kind=='changes' else 'white')
            draw.rectangle((40,y+8,44,y+h-8),fill=accent)
            for index,line in enumerate(lines):
                bold = index==0 and bool(re.match(r"\d+-й урок",line))
                draw.text((72,y+padding//2+index*line_height),line,font=face(size,bold),fill='#182230')
            y += h+gap
        draw.line((42,height-54,WIDTH-42,height-54),fill='#D0D5DD',width=1)
        draw.text((42,height-43),'Источник: rktm.info',font=face(20),fill='#667085')
        if len(pages)>1:
            draw.text((WIDTH-240,height-43),f'Лист {number} / {len(pages)}',font=face(20),fill='#667085')
        output = io.BytesIO()
        image.save(output,format='PNG',optimize=True)
        results.append(output.getvalue())
    return tuple(results)


async def daily_images(title, parts, changes=''):
    async with _RENDER_LIMIT:
        return await asyncio.to_thread(render_daily_pages, title, tuple(parts), changes)
