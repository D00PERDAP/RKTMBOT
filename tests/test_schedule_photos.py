"""Daily photo rendering and transport regressions, without sending live messages."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image
import features
import bot_flow as tg
import max_bot as mx
from schedule_images import content_blocks, layout_cards, render_daily_pages

FIXTURES = Path(__file__).parent / 'fixtures'
HTML = (FIXTURES / 'schedule-group.html').read_text(encoding='utf-8')
TITLE = json.loads((FIXTURES / 'schedule-group.json').read_text(encoding='utf-8'))['name']

class CardTests(unittest.TestCase):
    def test_real_timetable_retains_lesson_numbers_times_and_last_lesson(self):
        parts = tg.schedule_text_parts(TITLE, HTML, 0)
        heading, pages = layout_cards(parts, title=TITLE)
        content = '\n'.join(line for page in pages for _, lines in page for line in lines)
        self.assertIn('Понедельник',heading)
        self.assertIn('02 ноября',heading)
        for lesson in range(1,9):
            self.assertIn(f'{lesson}-й урок',content)
        self.assertIn('08:30-09:15',content)
        self.assertIn('15:25-16:10',content)
        for data in render_daily_pages(TITLE,tuple(parts)):
            with Image.open(io.BytesIO(data)) as image:
                self.assertEqual(image.format,'PNG')
                self.assertEqual(image.width,1080)
                self.assertLessEqual(image.height,1600)
                self.assertLess(len(data),10_000_000)
    def test_long_lesson_and_changes_are_paginated_without_clipping(self):
        parts=['📅 СВ-02 · Понедельник\n📆 02 ноября\n\n<b>11-й урок</b>\n' + 'ДлинноеНазвание'*500 + ' КОНЕЦ_УРОКА']
        changes='🔄 Замены на 02.11.2026\n\n🎓 СВ-02 · Урок 11\nБыло: Исходное\nСтало: Новое\nКабинет: 999'
        heading,pages=layout_cards(parts,changes,'СВ-02')
        self.assertGreater(len(pages),1)
        content='\n'.join(line for page in pages for _,lines in page for line in lines)
        self.assertIn('КОНЕЦ_УРОКА',content)
        self.assertIn('Кабинет: 999',content)
        self.assertNotIn('\u200d',content)
        self.assertIn('changes',[kind for page in pages for kind,_ in page])
    def test_real_day_and_four_substitutions_fit_one_readable_sheet(self):
        import substitutions as subs
        html=(FIXTURES/'schedule-with-changes.html').read_text(encoding='utf-8')
        rows=json.loads((FIXTURES/'replacements-2026-11-02.json').read_text(encoding='utf-8'))
        rows=[r for r in rows if r['group']=='СВ-04']
        self.assertEqual(len(rows),4)
        parts=tg.schedule_text_parts('СВ-04',html,0)
        changes=subs.format_day_changes(rows,'2026-11-02')
        images=render_daily_pages('СВ-04',tuple(parts),changes)
        self.assertEqual(len(images),1)
        with Image.open(io.BytesIO(images[0])) as image:
            self.assertLessEqual(image.height,2000)
        _,pages=layout_cards(parts,changes,'СВ-04')
        content='\n'.join(line for page in pages for _,lines in page for line in lines)
        for number in range(3,7):
            self.assertIn(f'{number}-й урок',content)
        self.assertIn('8-й урок',content)
        self.assertEqual(content.count('Стало:'),4)
        self.assertEqual(content.count('Кабинет: 35'),2)
        self.assertEqual(content.count('Кабинет: УМПроб'),2)

    def test_ambiguous_subgroups_and_different_original_are_not_overwritten(self):
        parts=('📅 СВ-02 · Понедельник\n\n1-й урок · 08:30\nМатематика Иванов А.В. 25',)
        changes='Замены на 02.11.2026\n\nСВ-02 · Урок 1\nБыло: Математика\nСтало: Физика\nКабинет: 26\n\nСВ-02 · Урок 1\nБыло: Математика\nСтало: Химия\nКабинет: 27'
        _,pages=layout_cards(parts,changes,'СВ-02')
        text='\n'.join(line for page in pages for _,lines in page for line in lines)
        self.assertIn('Математика Иванов А.В. 25',text)
        self.assertIn('Стало: Физика',text)
        self.assertIn('Стало: Химия',text)
        different='СВ-02 · Урок 1\nБыло: История\nСтало: География'
        _,pages=layout_cards(parts,different,'СВ-02')
        text='\n'.join(line for page in pages for _,lines in page for line in lines)
        self.assertIn('Математика Иванов А.В. 25',text)
        self.assertIn('Было: История',text)

    def test_empty_timetable_still_produces_readable_png(self):
        pages=render_daily_pages('СВ-02',tuple())
        self.assertEqual(len(pages),1)
        self.assertTrue(pages[0].startswith(b'\x89PNG'))

    def test_content_change_invalidates_image_cache(self):
        parts=('📅 СВ-02 · Понедельник\n\n1-й урок\nФизика',)
        self.assertNotEqual(render_daily_pages('СВ-02',parts,'Кабинет: 25'),
                            render_daily_pages('СВ-02',parts,'Кабинет: 26'))
    def test_real_teacher_empty_day_does_not_invent_lessons(self):
        html=(FIXTURES/'schedule-teacher.html').read_text(encoding='utf-8')
        parts=tg.schedule_text_parts('Агеев С.О.',html,0)
        heading,blocks=content_blocks(parts)
        self.assertIn('Понедельник',heading)
        self.assertIn('Пар не указано',' '.join(text for _,text in blocks))

class PhotoDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.previous=features.DB_PATH
        features.DB_PATH=Path(self.tmp.name)/'test.db'
        features.initialize(); tg.setup(); mx.init_max()
        mx._IMAGE_TOKENS.clear()
    def tearDown(self):
        features.DB_PATH=self.previous
        mx._IMAGE_TOKENS.clear()
        self.tmp.cleanup()
    def message(self):
        message=AsyncMock()
        message.chat.id=7; message.from_user.id=7; message.message_id=90
        message.answer.return_value=SimpleNamespace(message_id=100)
        message.answer_photo.return_value=SimpleNamespace(message_id=101)
        message.edit_media.return_value=SimpleNamespace(message_id=90)
        return message
    async def test_telegram_carousel_replaces_one_photo_without_sending_all_leaves(self):
        message=self.message()
        with patch('app.fetch_html',AsyncMock(return_value=HTML)),patch('schedule_images.daily_images',AsyncMock(return_value=(b'first',b'last'))),patch('substitutions.personal_message',return_value=''),patch.object(tg,'today_schedule_day',return_value=0):
            await tg.schedule_image(message,'raspisan','1.html',TITLE)
            self.assertEqual(message.answer_photo.await_count,1)
            self.assertEqual(message.answer_photo.call_args.args[0].data,b'first')
            markup=message.answer_photo.call_args.kwargs['reply_markup']
            self.assertTrue(any(b.callback_data.endswith(':0:1') for row in markup.inline_keyboard for b in row))
            await tg.schedule_image(message,'raspisan','1.html',TITLE,day=0,edit=True,page=1)
        self.assertEqual(message.answer_photo.await_count,1)
        self.assertEqual(message.edit_media.call_args.kwargs['media'].media.data,b'last')
        self.assertIn('2/2',message.edit_media.call_args.kwargs['media'].caption)
        message.answer.assert_not_awaited()
    async def test_telegram_photo_failure_falls_back_to_all_text(self):
        message=self.message()
        parts=['📅 СВ-02 · Понедельник\n\n1-й урок\n'+'длинное '*1200+'КОНЕЦ_РАСПИСАНИЯ']
        with patch('app.fetch_html',AsyncMock(return_value=HTML)), patch.object(tg,'schedule_text_parts',return_value=parts), patch('schedule_images.daily_images',AsyncMock(side_effect=RuntimeError('font missing'))), patch('substitutions.personal_message',return_value='КОНЕЦ_ЗАМЕН'), patch.object(tg,'show_screen',AsyncMock()) as screen:
            await tg.schedule_image(message,'raspisan','1.html',TITLE,day=0)
        text=screen.call_args.args[1]+''.join(call.args[0] for call in message.answer.await_args_list)
        self.assertIn('КОНЕЦ_РАСПИСАНИЯ',text)
        self.assertIn('КОНЕЦ_ЗАМЕН',text)
        self.assertGreater(len(text),9000)
        self.assertTrue(message.answer.call_args.kwargs['reply_markup'])
    async def test_inline_notification_updates_clicking_user_not_bot_author(self):
        features.save_user('telegram',7,'raspisan','1.html','СВ-02',True)
        features.save_user('telegram',999,'raspisan','2.html','СВ-03',True)
        call=AsyncMock();call.from_user.id=7;call.message.from_user.id=999;call.data='ui:notify:off'
        with patch.object(tg,'show_settings',AsyncMock()) as settings:
            await tg.interface_callback(call)
        self.assertFalse(features.user('telegram',7)['notifications'])
        self.assertTrue(features.user('telegram',999)['notifications'])
        self.assertEqual(settings.call_args.args[1],'7')
    async def test_telegram_catalog_old_button_keeps_original_item(self):
        html=''.join(f'<a href="{i}.html">СВ-{i:02}</a>' for i in range(1,18))
        message=self.message()
        with patch('app.fetch_html',AsyncMock(return_value=html)), patch.object(tg,'show_screen',AsyncMock()) as screen:
            await tg.show_catalog(message,'s',0)
            action=screen.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
            await tg.show_catalog(message,'s',1)
        call=AsyncMock();call.from_user.id=7;call.data=action
        with patch.object(tg,'account',return_value={'display_name':'Student'}),patch.object(tg,'show_item',AsyncMock()) as item:
            await tg.callback(call)
        self.assertEqual(item.call_args.args[3],'1.html')
    async def test_max_upload_uses_image_token_retries_not_ready_and_keeps_buttons(self):
        requests=[];attempts=0
        def transport(request):
            nonlocal attempts
            requests.append(request)
            if request.url.path=='/uploads':
                self.assertEqual(request.url.params['type'],'image')
                return httpx.Response(200,json={'url':'https://iu.oneme.ru/uploadImage?r=signed'})
            if request.url.host=='iu.oneme.ru':
                self.assertNotIn('Authorization',request.headers)
                self.assertIn(b'image/png',request.content)
                return httpx.Response(200,json={'photos':{'123':{'token':'image-token'}}})
            attempts+=1
            if attempts==1:
                return httpx.Response(400,json={'code':'attachment.not.ready'})
            return httpx.Response(200,json={'success':True})
        marker=mx.EDIT_TARGET.set('old-menu')
        try:
            async with httpx.AsyncClient(base_url=mx.API,headers={'Authorization':'test-secret'},transport=httpx.MockTransport(transport)) as client:
                with patch.object(mx.asyncio,'sleep',AsyncMock()):
                    await mx.send_photo_pages(client,'7',(b'png-data',),mx.back_home(),'СВ-02')
                self.assertEqual(await mx.upload_image(client,b'png-data'),'image-token')
        finally:
            mx.EDIT_TARGET.reset(marker)
        self.assertEqual(sum(r.url.path=='/uploads' for r in requests),1)
        sends=[r for r in requests if r.url.path=='/messages']
        self.assertEqual(len(sends),2)
        payload=json.loads(sends[-1].content)
        self.assertEqual(payload['attachments'][0]['type'],'image')
        self.assertEqual(payload['attachments'][0]['payload']['token'],'image-token')
        self.assertEqual(payload['attachments'][1]['type'],'inline_keyboard')
    async def test_max_image_failure_keeps_complete_timetable_in_text(self):
        selected={'source':'raspisan','filename':'1.html','title':TITLE}
        with patch.object(mx,'fetch_html',AsyncMock(return_value=HTML)),patch('schedule_images.daily_images',AsyncMock(return_value=(b'png',))),patch.object(mx,'send_photo_pages',AsyncMock(side_effect=RuntimeError('upload failed'))),patch.object(mx,'send',AsyncMock()) as send,patch('substitutions.personal_message',return_value=''),patch.object(mx,'today_schedule_day',return_value=0):
            await mx.show_schedule(AsyncMock(),'7',selected=selected)
        self.assertIn('8-й урок',send.call_args.args[2])
        self.assertTrue(send.call_args.args[3])

if __name__=='__main__': unittest.main()
