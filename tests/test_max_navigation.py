"""Navigation regressions for the MAX chat interface; no live messages."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import httpx
import features
import max_bot as m

class MaxNavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = features.DB_PATH
        features.DB_PATH = Path(self.tmp.name) / 'max.db'
        m.init_max()
    def tearDown(self):
        features.DB_PATH = self.previous
        self.tmp.cleanup()
    def register(self, uid='7', source='raspisan', filename='1.html', title='АТ-15', notifications=False):
        with features.db() as c:
            c.execute('INSERT INTO max_profiles VALUES(?,?,?,?,?)', (uid,source,filename,title,'student'))
        features.save_user('max', uid, source, filename, title, notifications)
    async def test_catalog_selection_is_stable_after_other_page_opened(self):
        items=''.join(f'<a href="{i}.html">АТ-{i:02}</a>' for i in range(1,13))
        with patch.object(m,'fetch_html',AsyncMock(return_value=items)), patch.object(m,'send',AsyncMock()) as send:
            await m.catalog(AsyncMock(),'7','raspisan',0)
            first_keyboard=send.call_args.args[3]
            first_action=first_keyboard['payload']['buttons'][0][0]['payload']
            await m.catalog(AsyncMock(),'7','raspisan',1)
            await m.handle(AsyncMock(),'7',first_action)
        self.assertEqual(m.profile('7')['filename'],'1.html')
    async def test_stale_selection_does_not_replace_profile(self):
        self.register()
        with patch.object(m,'fetch_html',AsyncMock(return_value='<a href="2.html">СВ-02</a>')), patch.object(m,'send',AsyncMock()):
            await m.handle(AsyncMock(),'7','/select raspisan removed.html')
        self.assertEqual(m.profile('7')['filename'],'1.html')
    async def test_favorite_open_and_day_do_not_change_profile_or_mute(self):
        self.register()
        with features.db() as c:
            c.execute("INSERT INTO max_favorites VALUES('7','Praspisan','m1.html','Гуркина Ю.А.')")
        with patch.object(m,'show_schedule',AsyncMock()) as show:
            await m.handle(AsyncMock(),'7','/favorite Praspisan m1.html')
            self.assertEqual(show.call_args.kwargs['selected']['title'],'Гуркина Ю.А.')
            await m.handle(AsyncMock(),'7','/favday Praspisan m1.html 4')
            self.assertEqual(show.call_args.args[2],4)
        self.assertEqual(m.profile('7')['filename'],'1.html')
        self.assertFalse(features.user('max',7)['notifications'])
    async def test_other_users_favorites_cannot_be_opened(self):
        self.register()
        with features.db() as c:
            c.execute("INSERT INTO max_favorites VALUES('7','Praspisan','m1.html','Гуркина Ю.А.')")
        with patch.object(m,'show_schedule',AsyncMock()) as show, patch.object(m,'send',AsyncMock()):
            await m.handle(AsyncMock(),'8','/favorite Praspisan m1.html')
            show.assert_not_awaited()
    async def test_toggle_persists_and_unregistered_user_is_not_told_it_succeeded(self):
        self.register()
        with patch.object(m,'send',AsyncMock()) as send:
            await m.handle(AsyncMock(),'7','/notify')
            self.assertTrue(features.user('max',7)['notifications'])
            self.assertIn('/mute',str(send.call_args))
            await m.handle(AsyncMock(),'7','/mute')
            self.assertFalse(features.user('max',7)['notifications'])
            await m.handle(AsyncMock(),'8','/notify')
            self.assertIsNone(features.user('max',8))
            self.assertIn('/reg_student',str(send.call_args))
    async def test_long_callback_edits_first_part_and_retains_menu_on_last(self):
        requests=[]
        def transport(request):
            requests.append(request)
            return httpx.Response(200,json={'success':True})
        token=m.EDIT_TARGET.set('menu-message')
        try:
            async with httpx.AsyncClient(base_url=m.API,transport=httpx.MockTransport(transport)) as client:
                await m.send(client,'7','a'*3500+'b'*100,m.back_home())
        finally:
            m.EDIT_TARGET.reset(token)
        import json
        self.assertEqual([r.method for r in requests],['PUT','POST'])
        self.assertEqual(json.loads(requests[0].content)['attachments'],[])
        self.assertTrue(json.loads(requests[1].content)['attachments'])
        self.assertEqual(''.join(json.loads(r.content)['text'] for r in requests),'a'*3500+'b'*100)

if __name__ == '__main__': unittest.main()
