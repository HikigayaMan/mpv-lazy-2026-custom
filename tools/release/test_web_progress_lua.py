"""Progress identity and quality-switch isolation using the production Lua script."""
from pathlib import Path
import unittest
try:
    from lupa.lua51 import LuaRuntime
except ImportError:
    LuaRuntime = None

SOURCE = Path(__file__).resolve().parents[2] / 'portable_config/scripts/web_progress.lua'
MOCK = '''
T = {props={}, events={}, timers={}, hooks={}, clock=0, opts={}, option_values={}}
mp = {}
function mp.get_property_native(k, d) if T.props[k] == nil then return d end return T.props[k] end
mp.get_property = mp.get_property_native
mp.get_property_number = mp.get_property_native
mp.get_property_bool = mp.get_property_native
function mp.set_property_native(k,v) T.props[k]=v end
function mp.get_time() return T.clock end
function mp.get_opt(k) return T.option_values[k] end
function mp.register_event(k,f) T.events[k]=f end
function mp.observe_property(k,t,f) T.events[k]=f end
function mp.add_hook(k,p,f) T.hooks[k]=f end
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

    def finals(self):
        return self.lua.globals().T.props['user-data/mpv-lazy/playback-progress-finals']

    def pause(self, value=True):
        self.lua.globals().T.props['pause'] = value
        self.lua.globals().T.events['pause']('pause', value)

    def test_ordinary_local_launch_without_identity_does_nothing(self):
        self.load(False)
        self.assertIsNone(self.state())
        self.assertIsNone(self.finals())
        self.assertEqual(len(self.lua.globals().T.timers), 0)

    def test_normal_mode_initializes_pause_queue_without_changing_api_version(self):
        self.load()
        self.assertEqual(len(self.finals()), 0)
        self.assertEqual(self.state()['apiVersion'], 1)
        self.assertEqual(self.state()['pauseEventsApiVersion'], 1)
        self.assertIsNone(self.state()['playlistId'])
        self.pause()
        self.assertEqual(len(self.finals()), 0)

    def test_quick_pause_resume_retains_independent_pause_boundary(self):
        self.load()
        self.player(pos=20.25)
        self.pause()
        self.pause(False)
        self.lua.execute("T.props['time-pos']=21.5; tick(); event('seek'); event('playback-restart')")
        self.assertEqual(self.state()['position'], 21.5)
        self.assertFalse(self.state()['paused'])
        self.assertEqual(len(self.finals()), 1)
        boundary = self.finals()[1]
        self.assertEqual(boundary['finalSequence'], 1)
        self.assertEqual(boundary['snapshot']['position'], 20.25)
        self.assertTrue(boundary['snapshot']['paused'])
        self.assertFalse(boundary['snapshot']['ended'])

    def test_paused_seek_updates_snapshot_without_adding_false_boundaries(self):
        self.load()
        self.player(pos=20)
        self.pause()
        self.lua.execute("T.props['time-pos']=7; event('seek'); tick()")
        self.assertEqual(self.state()['position'], 7)
        self.assertTrue(self.state()['paused'])
        self.assertEqual(len(self.finals()), 1)
        self.assertEqual(self.finals()[1]['snapshot']['position'], 20)

    def test_normal_pause_queue_is_bounded_with_monotonic_sequences(self):
        self.load()
        self.player()
        for position in range(1, 42):
            self.lua.globals().T.props['time-pos'] = position
            self.pause()
            self.pause(False)
        self.assertEqual(len(self.finals()), 32)
        self.assertEqual([record['finalSequence'] for record in self.finals().values()], list(range(10, 42)))
        self.assertEqual(self.finals()[1]['snapshot']['position'], 10)
        self.assertEqual(self.finals()[32]['snapshot']['position'], 41)

    def test_pause_during_quality_replacement_does_not_create_boundary(self):
        self.load()
        self.player(pos=35)
        self.lua.execute("T.props['user-data/mpv-lazy/progress-stream-switch']={sessionId='progress-test',identity='bilibili:123:456',from='video-a',to='video-b',expiresAt=15,resumePosition=35}")
        self.pause()
        self.assertEqual(len(self.finals()), 0)
        self.lua.execute("event('end-file','stop'); T.props.path='video-b'; T.props['time-pos']=0; event('start-file'); event('file-loaded')")
        self.pause(False)
        self.pause()
        self.assertEqual(len(self.finals()), 0)
        self.assertFalse(self.state()['ready'])
        self.assertEqual(self.state()['position'], 35)
        self.assertIsNone(self.state()['terminalReason'])

    def test_pause_in_foreign_file_does_not_overwrite_old_boundary(self):
        self.load()
        self.player(pos=25)
        self.pause()
        self.player('unrelated', 80, 150)
        self.pause(False)
        self.pause()
        self.lua.execute("event('seek'); tick()")
        self.assertFalse(self.state()['active'])
        self.assertEqual(len(self.finals()), 1)
        self.assertEqual(self.finals()[1]['snapshot']['identity'], 'bilibili:123:456')
        self.assertEqual(self.finals()[1]['snapshot']['position'], 25)

    def test_shutdown_retains_last_sample_without_extra_pause_boundary(self):
        self.load()
        self.player(pos=20)
        self.lua.execute("T.props['time-pos']=54.75; event('shutdown')")
        self.assertTrue(self.state()['ended'])
        self.assertEqual(self.state()['terminalReason'], 'shutdown')
        self.assertEqual(self.state()['position'], 54.75)
        self.pause()
        self.assertEqual(len(self.finals()), 0)

    def test_playlist_pause_boundary_uses_same_queue_and_rejects_quality_reload(self):
        playlist_id = 'playlist-' + 'a' * 32
        self.lua.globals().T.option_values['web_playlist-id'] = playlist_id
        self.load()
        path = f'http://127.0.0.1:9001/web-playlist/{playlist_id}/123:456'
        self.lua.globals().T.props['user-data/mpv-lazy/web-playlist/current'] = self.lua.table_from({
            'playlistId': playlist_id, 'sessionId': 'progress-test', 'path': path,
            'itemId': '123:456', 'identity': 'bilibili:123:456', 'generation': 1,
            'position': 20, 'duration': 100,
        })
        self.player(path, 20)
        self.pause()
        self.assertEqual(self.finals()[1]['snapshot']['playlistId'], playlist_id)
        self.lua.globals().T.props['user-data/mpv-lazy/web-playlist/quality-switch'] = self.lua.table_from({
            'playlistId': playlist_id, 'identity': 'bilibili:123:456', 'expiresAt': 15,
        })
        self.pause(False)
        self.pause()
        self.lua.execute("T.hooks['on_unload'](); event('end-file','stop')")
        self.assertEqual(len(self.finals()), 1)
        self.assertFalse(self.state()['ended'])
        self.assertIsNone(self.state()['terminalReason'])

    def test_quit_reason_is_explicit_without_treating_stop_or_eof_as_close(self):
        self.load()
        for reason in ('quit', 'stop', 'eof'):
            self.player(pos=35)
            self.lua.execute(f"event('end-file','{reason}')")
            self.assertTrue(self.state()['ended'])
            self.assertEqual(self.state()['terminalReason'], reason)
            self.assertEqual(self.state()['completed'], reason == 'eof')
            self.pause()
        self.assertEqual(len(self.finals()), 0)

    def test_new_file_clears_terminal_reason_and_preserves_foreign_file_isolation(self):
        self.load()
        self.player(pos=35)
        self.lua.execute("event('end-file','quit')")
        self.assertEqual(self.state()['terminalReason'], 'quit')
        self.player('unrelated', 70, 200)
        self.assertIsNone(self.state()['terminalReason'])
        self.assertFalse(self.state()['active'])
        self.assertFalse(self.state()['ready'])
        self.assertEqual(self.state()['position'], 35)

    def test_playlist_new_context_clears_previous_quit_reason(self):
        playlist_id = 'playlist-' + 'a' * 32
        self.lua.globals().T.option_values['web_playlist-id'] = playlist_id
        self.load()
        for generation, cid in ((1, 456), (2, 789)):
            path = f'http://127.0.0.1:9001/web-playlist/{playlist_id}/123:{cid}'
            self.lua.globals().T.props['user-data/mpv-lazy/web-playlist/current'] = self.lua.table_from({
                'playlistId': playlist_id, 'sessionId': 'progress-test', 'path': path,
                'itemId': f'123:{cid}', 'identity': f'bilibili:123:{cid}', 'generation': generation,
                'position': 20, 'duration': 100,
            })
            self.player(path, 20)
            self.assertIsNone(self.state()['terminalReason'])
            self.assertFalse(self.state()['ended'])
            self.lua.execute("event('end-file','quit')")
            self.assertEqual(self.state()['terminalReason'], 'quit')

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
        self.assertEqual(self.state()['terminalReason'], 'eof')
        self.assertEqual(self.state()['position'], 100)


if __name__ == '__main__':
    unittest.main()
