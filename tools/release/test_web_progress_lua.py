"""Progress identity and quality-switch isolation using the production Lua script."""
from pathlib import Path
import unittest
try:
    from lupa.lua51 import LuaRuntime
except ImportError:
    LuaRuntime = None

SOURCE = Path(__file__).resolve().parents[2] / 'portable_config/scripts/web_progress.lua'
MOCK = '''
T = {props={}, events={}, timers={}, clock=0, opts={}}
mp = {}
function mp.get_property_native(k, d) if T.props[k] == nil then return d end return T.props[k] end
mp.get_property = mp.get_property_native
mp.get_property_number = mp.get_property_native
mp.get_property_bool = mp.get_property_native
function mp.set_property_native(k,v) T.props[k]=v end
function mp.get_time() return T.clock end
function mp.register_event(k,f) T.events[k]=f end
function mp.observe_property(k,t,f) T.events[k]=f end
function mp.add_periodic_timer(s,f) T.timers[#T.timers+1]=f return {} end
package.preload['mp'] = function() return mp end
package.preload['mp.options'] = function() return {read_options=function(o) for k,v in pairs(T.opts) do o[k]=v end end} end
function event(k, reason) T.events[k]({reason=reason}) end
function tick() for _,f in ipairs(T.timers) do f() end end
'''


@unittest.skipIf(LuaRuntime is None, 'lupa.lua51 is required')
class WebProgressLuaTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute(MOCK)

    def load(self, enabled=True):
        if enabled:
            self.lua.execute("T.opts={identity='bilibili:123:456',session_id='progress-test'}")
        self.lua.execute(SOURCE.read_text(encoding='utf-8'))

    def player(self, path='video-a', pos=20, duration=100):
        self.lua.globals().T.props['path'] = path
        self.lua.globals().T.props['time-pos'] = pos
        self.lua.globals().T.props['duration'] = duration
        self.lua.execute("event('start-file'); event('file-loaded')")

    def state(self):
        return self.lua.globals().T.props['user-data/mpv-lazy/playback-progress']

    def test_ordinary_local_launch_without_identity_does_nothing(self):
        self.load(False)
        self.assertIsNone(self.state())
        self.assertEqual(len(self.lua.globals().T.timers), 0)

    def test_backward_and_zero_seek_are_retained(self):
        self.load()
        self.player()
        for pos in (7, 0):
            self.lua.globals().T.props['time-pos'] = pos
            self.lua.execute("event('seek')")
            self.assertEqual(self.state()['position'], pos)

    def test_legacy_combined_append_options_restore_separate_identity_and_session(self):
        self.lua.execute("T.opts={identity='bilibili:123:456,web_progress-session_id=progress-test'}")
        self.load(False)
        self.player()
        self.assertTrue(self.state()['ready'])
        self.assertEqual(self.state()['identity'], 'bilibili:123:456')
        self.assertEqual(self.state()['sessionId'], 'progress-test')

    def test_quality_replacement_preserves_identity_without_zero_write(self):
        self.load()
        self.player()
        self.lua.execute("T.props['user-data/mpv-lazy/progress-stream-switch']={sessionId='progress-test',identity='bilibili:123:456',from='video-a',to='video-b',expiresAt=15,resumePosition=20}; event('end-file','stop'); T.props.path='video-b'; T.props['time-pos']=0; event('start-file')")
        self.assertTrue(self.state()['active'])
        self.assertFalse(self.state()['ready'])
        self.assertEqual(self.state()['position'], 20)
        self.lua.execute("event('file-loaded'); tick()")
        self.assertFalse(self.state()['ready'])
        self.assertEqual(self.state()['position'], 20)
        self.lua.execute("T.props['time-pos']=20; event('playback-restart')")
        self.assertTrue(self.state()['ready'])
        self.assertEqual(self.state()['identity'], 'bilibili:123:456')

    def test_unrelated_file_invalidates_progress(self):
        self.load()
        self.player()
        self.player('unrelated', 80, 150)
        self.assertFalse(self.state()['active'])
        self.assertEqual(self.state()['position'], 20)

    def test_foreign_session_cannot_authorize_a_switch(self):
        self.load()
        self.player()
        self.lua.execute("T.props['user-data/mpv-lazy/progress-stream-switch']={sessionId='progress-other',identity='bilibili:123:456',from='video-a',to='video-b',expiresAt=15}")
        self.player('video-b', 50)
        self.assertFalse(self.state()['active'])

    def test_expired_switch_stops_loading_session(self):
        self.load()
        self.player()
        self.lua.execute("T.props['user-data/mpv-lazy/progress-stream-switch']={sessionId='progress-test',identity='bilibili:123:456',from='video-a',to='video-b',expiresAt=15}; event('end-file','stop'); T.clock=16; tick()")
        self.assertFalse(self.state()['active'])
        self.assertTrue(self.state()['ended'])

    def test_eof_records_completion_and_final_duration(self):
        self.load()
        self.player(pos=99)
        self.lua.execute("T.props['time-pos']=nil; event('end-file','eof')")
        self.assertTrue(self.state()['completed'])
        self.assertTrue(self.state()['ended'])
        self.assertEqual(self.state()['position'], 100)


if __name__ == '__main__':
    unittest.main()
