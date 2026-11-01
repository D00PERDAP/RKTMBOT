"""Two-day substitution window and one-message photo carousel regressions."""
from datetime import date,datetime,timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock,patch
import features
import substitutions as subs
import bot_flow as tg
import max_bot as mx

DAYS=['2026-11-02','2026-11-03']

class DateWindowTests(unittest.TestCase):
    def test_window_crosses_year_boundary(self):
        self.assertEqual(subs.relevant_days(date(2026,12,31)),['2026-12-31','2027-01-01'])
    def test_college_day_has_rolled_over_while_utc_day_has_not(self):
        class Frozen(datetime):
            @classmethod
            def now(cls,tz=None):
                return datetime(2026,11,1,21,30,tzinfo=timezone.utc).astimezone(tz)
        with patch.object(subs,'datetime',Frozen),patch.dict('os.environ',{'SCHEDULE_TIMEZONE':'Europe/Moscow'}):
            self.assertEqual(subs.relevant_days(),DAYS)

class CarouselNavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.old=features.DB_PATH
        features.DB_PATH=Path(self.tmp.name)/'test.db'
        features.initialize();tg.setup();mx.init_max()
        with features.db() as c:
            for day in ['2025-01-01','2027-01-01']:
                c.execute('INSERT INTO substitution_documents VALUES(?,?,?)',(day,day,'hash'))
    def tearDown(self):
        features.DB_PATH=self.old;self.tmp.cleanup()
    def message(self):
        message=AsyncMock();message.chat.id=7;message.from_user.id=7
        message.answer.return_value=SimpleNamespace(message_id=10)
        return message
    async def test_both_date_menus_ignore_archive_and_keep_unpublished_tomorrow(self):
        with patch.object(subs,'relevant_days',return_value=DAYS):
            markup=tg.substitution_dates()
            actions=[b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data.startswith('subs:day:')]
            self.assertEqual(actions,['subs:day:'+day for day in DAYS])
            with patch.object(mx,'send',AsyncMock()) as send:
                await mx.handle(AsyncMock(),'7','/subsdates')
                buttons=send.call_args.args[3]['payload']['buttons']
                actions=[b['payload'] for row in buttons for b in row if b['payload'].startswith('/subsday ')]
                self.assertEqual(actions,['/subsday '+day for day in DAYS])
                await mx.handle(AsyncMock(),'7','/subsday '+DAYS[1])
                self.assertIn('Опубликованных изменений пока нет',send.call_args.args[2])
    async def test_stale_archive_date_refreshes_choices_in_both_bots(self):
        call=AsyncMock();call.from_user.id=7;call.data='subs:day:2025-01-01'
        with patch.object(subs,'relevant_days',return_value=DAYS),patch.object(tg,'show_screen',AsyncMock()) as screen:
            await tg.substitution_callback(call)
            self.assertIn('сегодня или завтра',screen.call_args.args[1])
        with patch.object(subs,'relevant_days',return_value=DAYS),patch.object(mx,'send',AsyncMock()) as send:
            await mx.handle(AsyncMock(),'7','/subsday 2025-01-01')
            self.assertIn('сегодня или завтра',send.call_args.args[2])
    async def test_telegram_leaf_callback_preserves_day_and_page(self):
        features.save_user('telegram',7,'raspisan','1.html','СВ-02',False)
        item_id=tg.catalog_item_ids('raspisan',[{'file':'1.html','name':'СВ-02'}])[0]
        call=AsyncMock();call.from_user.id=7;call.data=f'sheet:{item_id}:0:1'
        with patch.object(tg,'schedule_image',AsyncMock()) as image:
            await tg.callback(call)
        self.assertEqual(image.call_args.kwargs['day'],0)
        self.assertEqual(image.call_args.kwargs['page'],1)
        self.assertTrue(image.call_args.kwargs['edit'])
    async def test_max_uploads_only_selected_leaf_and_edits_current_message(self):
        marker=mx.EDIT_TARGET.set('existing-photo')
        client=AsyncMock()
        try:
            with patch.object(mx,'upload_image',AsyncMock(return_value='token')) as upload,patch.object(mx,'image_request',AsyncMock()) as request:
                await mx.send_photo_pages(client,'7',(b'first',b'last'),mx.back_home(),'СВ-02',page=1)
        finally:
            mx.EDIT_TARGET.reset(marker)
        upload.assert_awaited_once_with(client,b'last')
        self.assertEqual(request.call_args.args[1],'PUT')
        self.assertEqual(request.call_args.args[2],{'message_id':'existing-photo'})
        self.assertIn('2/2',request.call_args.args[3]['text'])
    async def test_max_favorite_carousel_is_owned_and_never_changes_profile(self):
        with features.db() as c:
            c.execute("INSERT INTO max_profiles VALUES('7','raspisan','1.html','СВ-02','student')")
            c.execute("INSERT INTO max_favorites VALUES('7','Praspisan','m1.html','Агеев С.О.')")
        features.save_user('max',7,'raspisan','1.html','СВ-02',False)
        command='/sheet Praspisan m1.html 0 1 f'
        with patch.object(mx,'show_schedule',AsyncMock()) as show,patch.object(mx,'send',AsyncMock()):
            await mx.handle(AsyncMock(),'7',command)
            self.assertEqual(show.call_args.kwargs['page'],1)
            self.assertEqual(show.call_args.kwargs['selected']['filename'],'m1.html')
            show.reset_mock()
            await mx.handle(AsyncMock(),'8',command)
            show.assert_not_awaited()
        self.assertEqual(mx.profile('7')['filename'],'1.html')
        self.assertFalse(features.user('max',7)['notifications'])
    async def test_max_old_personal_carousel_does_not_switch_saved_profile(self):
        with features.db() as c:
            c.execute("INSERT INTO max_profiles VALUES('7','raspisan','2.html','СВ-03','student')")
        with patch.object(mx,'show_schedule',AsyncMock()) as show,patch.object(mx,'send',AsyncMock()) as send:
            await mx.handle(AsyncMock(),'7','/sheet raspisan 1.html 0 1 p')
            show.assert_not_awaited()
            self.assertIn('изменилось',send.call_args.args[2])

if __name__=='__main__': unittest.main()
