"""Offline regression tests. Fixtures are unmodified public college PDFs."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import features
import substitutions as s

FIXTURES = Path(__file__).parent / 'fixtures'

class ParsingTests(unittest.TestCase):
    def test_real_tables(self):
        for name, count in [('2026-10-28', 18), ('2026-11-02', 14)]:
            with self.subTest(day=name):
                path = FIXTURES / f'replacements-{name}.pdf'
                rows = s.parse_pdf(path.read_bytes(), name)
                self.assertEqual(len(rows), count)
                self.assertEqual(rows, json.loads(path.with_suffix('.json').read_text(encoding='utf-8')))
                self.assertTrue(all(1 <= r['lesson'] <= 12 for r in rows))
    def test_groups(self):
        self.assertEqual(s.group_key('АТ09.29-15'), s.group_key('АТ – 15'))
        self.assertNotEqual(s.group_key('ОЛ-29'), s.group_key('ОЛ-29к'))
        self.assertIsNone(s.group_key(''))
    def test_teachers(self):
        self.assertTrue(s.teacher_matches('Гуркина Ю.А.,', 'Ин. язык Гуркина Ю.А.'))
        self.assertFalse(s.teacher_matches('Гуркина Ю.А.', 'Гуркина Ю.В.'))
        self.assertFalse(s.teacher_matches('Ким', 'Климова Ю.В.'))
        self.assertTrue(s.teacher_matches('Ким И.А.', 'Ким (10 ур.)'))
        self.assertTrue(s.teacher_matches('Семёнов А.В.', 'Семенов А. В.'))
    def test_discovery(self):
        html = '<a href="/Замены-02.11.26.pdf?v=2">Замены</a><a href="https://evil.test/Замены-03.11.26.pdf">Замены</a><a href="/Замены.xlsx">Замены</a>'
        self.assertEqual([d['date'] for d in s.discover_pdfs(html)], ['2026-11-02'])

class StoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = features.DB_PATH
        features.DB_PATH = Path(self.tmp.name) / 'test.db'
        features.initialize(); s.setup_store()
        self.day = '2026-11-02'
        self.data = (FIXTURES / 'replacements-2026-11-02.pdf').read_bytes()
    def tearDown(self):
        features.DB_PATH = self.old
        self.tmp.cleanup()
    async def refresh(self, data=None):
        with patch.object(s, 'fetch_documents', AsyncMock(return_value=[{'url':'https://rktm.info/test.pdf','date':self.day}])), patch.object(s, 'download_pdf', AsyncMock(return_value=data or self.data)):
            return await s.refresh()
    async def test_refresh_change_and_failed_parse_preserve_data(self):
        self.assertEqual(await self.refresh(), {self.day})
        self.assertEqual(await self.refresh(), set())
        self.assertEqual(len(s.substitutions_on(self.day)), 14)
        with patch.object(s, 'parse_pdf', return_value=[]):
            self.assertEqual(await self.refresh(self.data + b'changed'), set())
        self.assertEqual(len(s.substitutions_on(self.day)), 14)
        self.assertEqual(s.published_dates(), [self.day])
    async def test_targeting_retry_mute_and_restart(self):
        await self.refresh()
        row = s.substitutions_on(self.day)[0]
        features.save_user('telegram', 1, 'raspisan', '1.html', row['group'], True)
        features.save_user('telegram', 2, 'raspisan', '2.html', row['group'], False)
        features.save_user('telegram', 3, 'raspisan', '3.html', 'НЕ-99', True)
        bot = AsyncMock()
        bot.send_message.side_effect = RuntimeError('temporary')
        await s.notify_changed([self.day], bot)
        bot.send_message.reset_mock(); bot.send_message.side_effect = None
        await s.notify_changed([self.day], bot)
        self.assertEqual(bot.send_message.call_count, 1)
        self.assertEqual(bot.send_message.call_args.args[0], 1)
        self.assertNotIn('НЕ-99', bot.send_message.call_args.args[1])
        s.setup_store()
        await s.notify_changed([self.day], bot)
        self.assertEqual(bot.send_message.call_count, 1)
        with features.db() as c:
            c.execute('UPDATE substitution_rows_v2 SET room=? WHERE group_code=?', ('999',row['group']))
        await s.notify_changed([self.day], bot)
        self.assertEqual(bot.send_message.call_count, 2)
    async def test_partial_delivery_resume_and_content_reversion(self):
        features.save_user('telegram',1,'raspisan','1.html','АТ-15',True)
        with features.db() as c:
            c.execute('INSERT INTO substitution_rows_v2 VALUES(?,?,?,?,?,?,?)', ('u',self.day,'АТ-15',1,'a'*2000,'b'*2000,'25'))
        bot=AsyncMock()
        bot.send_message.side_effect=[None,RuntimeError('second part failed')]
        await s.notify_changed([self.day],bot)
        self.assertEqual(bot.send_message.call_count,2)
        failed_part=bot.send_message.call_args.args[1]
        bot.send_message.reset_mock();bot.send_message.side_effect=None
        await s.notify_changed([self.day],bot)
        self.assertEqual(bot.send_message.call_count,1)
        self.assertEqual(bot.send_message.call_args.args[1],failed_part)
        with features.db() as c:
            c.execute("UPDATE substitution_rows_v2 SET replacement='changed'")
        await s.notify_changed([self.day],bot)
        with features.db() as c:
            c.execute('UPDATE substitution_rows_v2 SET replacement=?',('b'*2000,))
        await s.notify_changed([self.day],bot)
        self.assertEqual(bot.send_message.call_count,4)

    async def test_style_change_and_legacy_receipt_do_not_resend_unchanged_rows(self):
        await self.refresh()
        group=s.substitutions_on(self.day)[0]['group']
        features.save_user('telegram',1,'raspisan','1.html',group,True)
        rows=s.personal_rows(features.user('telegram',1),self.day)
        old=s.digest(s.legacy_notification(rows,self.day).encode())
        with features.db() as c:
            c.execute('INSERT INTO substitution_deliveries VALUES(?,?,?,?)',('telegram','1',self.day,old))
        bot=AsyncMock()
        await s.notify_changed([self.day],bot)
        bot.send_message.assert_not_awaited()
        with patch.object(s,'format_day_changes',return_value='Другое оформление того же содержания'):
            await s.notify_changed([self.day],bot)
        bot.send_message.assert_not_awaited()

    async def test_corrected_url_and_reversion(self):
        await self.refresh()
        async def publish(url):
            with patch.object(s,'fetch_documents',AsyncMock(return_value=[{'url':url,'date':self.day}])), patch.object(s,'download_pdf',AsyncMock(return_value=self.data)):
                await s.refresh()
        await publish('https://rktm.info/corrected.pdf')
        with features.db() as c:
            self.assertEqual(c.execute('SELECT DISTINCT url FROM substitution_rows_v2').fetchone()[0], 'https://rktm.info/corrected.pdf')
        await publish('https://rktm.info/test.pdf')
        self.assertEqual(len(s.substitutions_on(self.day)),14)
        with features.db() as c:
            self.assertEqual(c.execute('SELECT DISTINCT url FROM substitution_rows_v2').fetchone()[0], 'https://rktm.info/test.pdf')

    async def test_teacher_both_sides(self):
        with features.db() as c:
            c.execute('INSERT INTO substitution_rows_v2 VALUES(?,?,?,?,?,?,?)', ('u',self.day,'АТ-15',1,'Ин.яз Гуркина Ю.А.','География Климова Ю.В.','25'))
        for name in ['Гуркина Ю.А.,', 'Климова Ю.В.']:
            self.assertEqual(len(s.personal_rows({'source':'Praspisan','group_name':name},self.day)),1)
        self.assertEqual(s.personal_rows({'source':'Praspisan','group_name':'Климова Ю.А.'},self.day),[])
    async def test_max_migration_and_registration_preserve_mute(self):
        from max_bot import init_max, choose
        init_max()
        features.save_user('max', '7', 'raspisan', 'old.html', 'АТ-15', False)
        with features.db() as c:
            c.execute("INSERT INTO max_profiles VALUES('7','raspisan','old.html','АТ-15','student')")
            c.execute("INSERT INTO max_states VALUES('7','Praspisan',0)")
        init_max()
        self.assertEqual(features.user('max',7)['notifications'],0)
        with patch('max_bot.fetch_html', AsyncMock(return_value='<a href="m1.html">Гуркина Ю.А.</a>')), patch('max_bot.send', AsyncMock()):
            await choose(AsyncMock(), '7', 1)
        p=features.user('max',7)
        self.assertEqual(p['source'],'Praspisan')
        self.assertEqual(p['notifications'],0)
    async def test_telegram_toggle(self):
        from bot_flow import notification_setting
        features.save_user('telegram',1,'raspisan','1.html','АТ-15',False)
        msg=AsyncMock(); msg.from_user.id=1; msg.text='/notify'
        with patch('bot_flow.show_settings', AsyncMock()):
            await notification_setting(msg)
            self.assertEqual(features.user('telegram',1)['notifications'],1)
            msg.text='/mute'; await notification_setting(msg)
            self.assertEqual(features.user('telegram',1)['notifications'],0)
    async def test_max_delivery_api_failure_and_success(self):
        await self.refresh()
        group=s.substitutions_on(self.day)[0]['group']
        features.save_user('max',7,'raspisan','1.html',group,True)
        import httpx
        calls=[]
        def transport(request):
            calls.append(request)
            return httpx.Response(200,json={'success':len(calls)>1})
        factory=s.httpx.AsyncClient
        with patch.dict('os.environ',{'MAX_BOT_TOKEN':'test'}), patch.object(s.httpx,'AsyncClient',lambda **kw: factory(transport=httpx.MockTransport(transport),**kw)):
            await s.notify_changed([self.day]); await s.notify_changed([self.day]); await s.notify_changed([self.day])
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[0].url.params['user_id'],'7')

    async def test_multiple_subgroups_and_legacy_migration(self):
        with features.db() as c:
            c.execute("CREATE TABLE substitution_rows(url,day,group_code,lesson,original,replacement,room)")
            c.execute("INSERT INTO substitution_rows VALUES('old',?,'АТ-15',1,'Исходный','Новый','25')",(self.day,))
            c.execute("DELETE FROM substitution_migrations WHERE name='rows_v2'")
        s.setup_store()
        with features.db() as c:
            c.execute("INSERT INTO substitution_rows_v2 VALUES('old',?,'АТ-15',1,'Исходный','Другая подгруппа','26')",(self.day,))
        self.assertEqual(len(s.substitutions_for('АТ-15', self.day)),2)
        with features.db() as c:
            c.execute("DELETE FROM substitution_rows_v2")
        s.setup_store()
        self.assertEqual(s.substitutions_on(self.day),[])

    async def test_max_teacher_menu_and_published_dates(self):
        from max_bot import init_max, handle
        init_max(); await self.refresh()
        with features.db() as c:
            c.execute("INSERT INTO max_profiles VALUES('7','Praspisan','m1.html','Ким И.А.','teacher')")
        with patch('max_bot.send', AsyncMock()) as send:
            await handle(AsyncMock(), '7', '/subsdates')
            self.assertIn(self.day, str(send.call_args))
            await handle(AsyncMock(), '7', '/subsmine')
            self.assertNotIn('Сначала выбери', send.call_args.args[2])
    async def test_telegram_onboarding_teacher(self):
        import bot_flow as flow
        flow.setup()
        flow.set_state('8', 'onboard:t')
        call=AsyncMock(); call.from_user.id=8; call.data='onboardpick:t:0:0'
        # Exercise the actual callback handler selected by aiogram's router.
        handler=next(h.callback for h in flow.router.callback_query.handlers
                     if h.callback.__name__ == 'onboarding')
        with patch.object(flow, 'resolve_item', AsyncMock(return_value={'file':'m1.html','name':'Гуркина Ю.А.'})), patch.object(flow, 'schedule_image', AsyncMock()):
            await handler(call)
        self.assertEqual(features.user('telegram',8)['source'],'Praspisan')
        self.assertEqual(flow.get_role('8'),'teacher')

if __name__ == '__main__': unittest.main()
