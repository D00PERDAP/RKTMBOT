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
MAX_HEIGHT = 1600
BODY_WIDTH = 900
LINE_HEIGHT = 44
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
    title_lines = wrap(draw, title, face(44, True), 940)
    # The full name remains in the original heading if unusually long.
    if len(title_lines) > 3:
        title_lines = ['Расписание на день']
    elif heading:
        first, *rest = heading.splitlines()
        if first.startswith(title):
            first = first[len(title):].strip(' ,·')
        heading = '\n'.join([first] + rest)
    metadata = wrap(draw, heading, face(26), 940) if heading else []
    height = 145 + len(title_lines)*58 + len(metadata)*36
    return title_lines, metadata, height


def layout_cards(parts, changes='', title=''):
    """Return every wrapped line, paginated by measured height; never truncate."""
    draw = ImageDraw.Draw(Image.new('RGB', (WIDTH, 1)))
    heading, blocks = content_blocks(parts, changes)
    cards = []
    for kind, text in blocks:
        lines = wrap(draw, text, face(32), BODY_WIDTH)
        for offset in range(0, len(lines), 20):
            segment = lines[offset:offset+20]
            if offset:
                segment = ['Продолжение'] + segment
            cards.append((kind, segment))
    _, _, header = header_layout(title, heading, draw)
    available = MAX_HEIGHT - header - 100
    if available < 1000:
        raise ValueError('Schedule title is too long for a photo; use text')
    pages, page, used = [], [], 0
    for card in cards:
        height = 48 + LINE_HEIGHT * len(card[1])
        if page and used + height + 16 > available:
            pages.append(page)
            page, used = [], 0
        page.append(card)
        used += height + 16
    if page:
        pages.append(page)
    return heading, pages


@lru_cache(maxsize=12)
def render_daily_pages(title, parts, changes=''):
    heading, pages = layout_cards(parts, changes, title)
    measure = ImageDraw.Draw(Image.new('RGB', (WIDTH, 1)))
    title_lines, metadata, header = header_layout(title, heading, measure)
    results = []
    for number, cards in enumerate(pages, 1):
        height = header + sum(48 + LINE_HEIGHT * len(lines) + 16 for _, lines in cards) + 100
        if height > MAX_HEIGHT:
            raise ValueError('Schedule page exceeds photo dimensions')
        image = Image.new('RGB', (WIDTH, height), '#F3F5F9')
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, WIDTH, 12), fill='#BE2440')
        draw.rounded_rectangle((48, 40, 192, 89), radius=14, fill='#BE2440')
        draw.text((66, 49), 'РКТМ', font=face(24, True), fill='white')
        draw.text((218, 51), 'РАСПИСАНИЕ', font=face(22, True), fill='#667085')
        y = 111
        for line in title_lines:
            draw.text((52, y), line, font=face(44, True), fill='#182230')
            y += 58
        for line in metadata:
            draw.text((52, y+3), line, font=face(26), fill='#667085')
            y += 36
        y = header
        for kind, lines in cards:
            h = 48 + LINE_HEIGHT * len(lines)
            accent = '#BE2440' if kind == 'changes' else '#3661A6'
            draw.rounded_rectangle((48, y, WIDTH-48, y+h), radius=18, fill='#FFF2F4' if kind == 'changes' else 'white')
            draw.rounded_rectangle((48, y+16, 54, y+h-16), radius=3, fill=accent)
            for index, line in enumerate(lines):
                bold = index == 0 and bool(re.match(r"\d+-й урок", line))
                draw.text((82, y+22+index*LINE_HEIGHT), line, font=face(32, bold), fill='#182230')
            y += h + 16
        draw.line((52, height-76, WIDTH-52, height-76), fill='#D0D5DD', width=1)
        draw.text((52, height-59), 'Источник: rktm.info', font=face(23), fill='#667085')
        draw.text((WIDTH-260, height-59), f'Страница {number} / {len(pages)}', font=face(23), fill='#667085')
        output = io.BytesIO()
        image.save(output, format='PNG', optimize=True)
        results.append(output.getvalue())
    return tuple(results)


async def daily_images(title, parts, changes=''):
    async with _RENDER_LIMIT:
        return await asyncio.to_thread(render_daily_pages, title, tuple(parts), changes)
