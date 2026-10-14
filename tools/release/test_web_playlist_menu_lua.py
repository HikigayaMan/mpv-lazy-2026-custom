"""Tree menu controller behavior with MPV properties and a persistent fake Menu."""
from __future__ import annotations

import json
from pathlib import Path
import unittest

import test_web_playlist_tree_lua as tree_tests

LuaRuntime = tree_tests.LuaRuntime

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "portable_config/scripts/uosc/lib/web_playlist_menu.lua"
TREE = ROOT / "portable_config/scripts/uosc/lib/web_playlist_tree.lua"
MANIFEST = "user-data/mpv-lazy/web-playlist-manifest"
STATUS = "user-data/uosc/web-playlist-menu"
METADATA_STATE = "user-data/mpv-lazy/web-playlist/metadata-state"

MOCK = r'''
T={props={}, observers={}, messages={}, commands={}, openCount=0, updateCount=0}
mp={}
function mp.get_property_native(k,d) if T.props[k]==nil then return d end return T.props[k] end
function mp.set_property_native(k,v) T.props[k]=v end
function mp.commandv(...) T.commands[#T.commands+1]={...} end
function mp.observe_property(k,kind,f)
    T.observers[k]=T.observers[k] or {}; table.insert(T.observers[k],f); f(k,T.props[k])
end
function mp.unobserve_property(f)
    for _,callbacks in pairs(T.observers) do
        for index=#callbacks,1,-1 do if callbacks[index]==f then table.remove(callbacks,index) end end
    end
end
function mp.register_script_message(k,f) T.messages[k]=f end
function T.fire(k)
    local callbacks={}; for _,f in ipairs(T.observers[k] or {}) do callbacks[#callbacks+1]=f end
    for _,f in ipairs(callbacks) do f(k,T.props[k]) end
end
Menu={}
function Menu:is_open(kind)
    return T.menu and not T.menu.destroyed and (not kind or T.menu.type==kind) and T.menu or nil
end
function Menu:close()
    local target=self==Menu and T.menu or self
    if target and not target.destroyed then
        target.destroyed=true
        if T.menu==target then T.menu=nil end
        if target.callback then target.callback({type='close'}) end
    end
end
function Menu:open(data,callback)
    if T.menu then T.menu:close() end
    local menu={type=data.type, data=data, callback=callback,
        current={items=data.items,scroll_y=23}, destroyed=false}
    function menu:update(data)
        T.updateCount=T.updateCount+1; self.data=data; self.current.items=data.items
    end
    function menu:select_value(value)
        self.current.selected_index=nil
        for index,item in ipairs(self.current.items) do
            if item.value==value then self.current.selected_index=index; break end
        end
    end
    function menu:scroll_to_index(index,menu_id,immediate)
        self.lastScrolledIndex=index; self.scrollImmediate=immediate
    end
    menu.close=Menu.close
    T.menu=menu; T.openCount=T.openCount+1
    return menu
end
ulang={_playlist_submenu_title='Playlist'}
package.loaded['mp']=mp
package.loaded['mp.utils']={parse_json=function(raw) return _parse_json(raw) end}
'''


@unittest.skipIf(LuaRuntime is None, "lupa.lua51 is required")
class WebPlaylistMenuLuaTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.g = self.lua.globals()
        self.g._parse_json = self.parse_json
        self.lua.execute(MOCK)
        self.t = self.g.T
        self.lua.globals()._tree = self.lua.execute(TREE.read_text(encoding="utf-8"))
        self.lua.execute("package.loaded['lib/web_playlist_tree']=_tree")
        # Fixture helpers share the real tree suite's public metadata contract.
        self.fixture = tree_tests.WebPlaylistTreeLuaTests()
        self.items = [self.fixture.item(cid=2001, p=1, part_count=3, order=1, section="one"),
                      self.fixture.item(cid=2002, p=2, part_count=3, order=2, section="one"),
                      self.fixture.item(cid=2003, p=3, part_count=3, order=3, section="one"),
                      self.fixture.item(1002, 3001, order=4, section="two")]
        self.native = self.fixture.native(self.items, current="1001:2002")
        self.t.props[MANIFEST] = self.to_lua(self.fixture.manifest(self.items))
        self.t.props.playlist = self.to_lua(self.native)
        self.controller = self.lua.execute(SOURCE.read_text(encoding="utf-8"))

    def to_lua(self, value):
        return self.lua.table_from(value, recursive=True) if isinstance(value, (dict, list)) else value

    def parse_json(self, raw):
        try:
            return self.to_lua(json.loads(raw))
        except (ValueError, TypeError):
            return None

    def rows(self):
        return list(self.t.menu.current["items"].values())

    def row(self, title):
        return next(row for row in self.rows() if row.title == title)

    def selected_value(self):
        index = self.t.menu.current.selected_index
        return self.t.menu.current["items"][index].value if index else None

    def activate(self, value):
        self.t.messages["web-playlist-menu-action"](json.dumps({"type": "activate", "value": value}))

    def commands(self):
        return [list(value.values()) for value in self.t.commands.values()]

    def observer_count(self):
        return sum(len(value) for value in self.t.observers.values())

    def test_current_leaf_ancestors_open_while_other_section_remains_folded(self):
        self.assertTrue(self.controller.open())
        self.assertIn("Part 2", [row.title for row in self.rows()])
        self.assertNotIn("Video 1002", [row.title for row in self.rows()])
        self.assertEqual(self.selected_value(), self.row("Part 2").value)
        self.assertTrue(self.row("Part 2").active)
        self.assertEqual(self.row("Part 2").depth, 2)

    def test_group_toggle_changes_visible_rows_without_native_player_command(self):
        self.controller.open()
        section = self.row("Section two").value
        before = len(self.rows())
        self.activate(section)
        self.assertEqual(len(self.rows()), before + 1)
        self.assertIn("Video 1002", [row.title for row in self.rows()])
        self.assertEqual(self.commands(), [])
        self.activate(section)
        self.assertEqual(len(self.rows()), before)
        self.assertEqual(self.commands(), [])

    def test_leaf_activation_uses_live_native_entry_id_after_reordering(self):
        self.controller.open()
        value = self.row("Part 2").value
        # Deliver the click before an observer refresh, as a real queued event
        # can refer to the previous rendered order during playlist-shuffle.
        self.t.props.playlist = self.to_lua([self.native[1], self.native[3], self.native[0], self.native[2]])
        self.activate(value)
        self.assertEqual(self.commands(), [["set", "playlist-pos", "0"]])

    def test_leaf_removed_before_queued_click_does_not_play_its_old_index(self):
        self.controller.open()
        value = self.row("Part 2").value
        self.t.props.playlist = self.to_lua([self.native[0], self.native[2], self.native[3]])
        self.activate(value)
        self.assertEqual(self.commands(), [])

    def test_fold_and_metadata_refresh_keep_focus_on_stable_group_value(self):
        self.controller.open()
        video = self.row("Video 1001").value
        self.activate(video)
        self.assertEqual(self.selected_value(), video)
        self.assertNotIn("Part 2", [row.title for row in self.rows()])
        manifest = self.fixture.manifest(self.items)
        manifest["revision"] = 2
        manifest["collectionTitle"] = "Updated title"
        self.t.props[MANIFEST] = self.to_lua(manifest)
        self.t.fire(MANIFEST)
        self.assertEqual(self.selected_value(), video)
        self.assertNotIn("Part 2", [row.title for row in self.rows()])
        self.assertIn("Updated title", self.t.menu.data.title)
        self.assertEqual(self.t.menu.current.scroll_y, 23)

    def test_close_removes_runtime_observers_and_published_support_state(self):
        baseline = self.observer_count()
        self.controller.open()
        self.assertEqual(self.observer_count(), baseline + 3)
        self.assertIsNotNone(self.t.props[STATUS])
        self.g.Menu.close(self.g.Menu)
        # The two permanent upgrade observers intentionally remain registered.
        self.assertEqual(self.observer_count(), baseline)
        self.assertIsNone(self.t.props[STATUS])
        self.assertIsNone(self.t.menu)

    def test_normal_playlist_without_hierarchy_uses_flat_menu_fallback(self):
        self.t.props[MANIFEST] = None
        self.assertFalse(self.controller.open())
        self.assertEqual(self.t.openCount, 0)
        self.assertIsNone(self.t.props[STATUS])

    def test_manifest_for_other_playlist_does_not_replace_normal_menu(self):
        manifest = self.fixture.manifest(self.items)
        manifest["playlistId"] = "playlist-" + "b" * 32
        self.t.props[MANIFEST] = self.to_lua(manifest)
        self.assertFalse(self.controller.open())
        self.assertEqual(self.t.openCount, 0)

    def test_new_current_item_opens_its_section_without_unfolding_previous_manual_fold(self):
        self.controller.open()
        video = self.row("Video 1001").value
        self.activate(video)
        native = [{**entry, "current": index == 3} for index, entry in enumerate(self.native)]
        self.t.props.playlist = self.to_lua(native)
        self.t.fire("playlist")
        self.assertIn("Video 1002", [row.title for row in self.rows()])
        self.assertNotIn("Part 2", [row.title for row in self.rows()])
        self.assertTrue(self.row("Video 1002").active)

    def test_group_remove_action_is_ignored_and_never_reaches_native_playlist(self):
        self.controller.open()
        section = self.row("Section one")
        self.assertEqual(len(section.actions), 0)
        self.t.menu.callback(self.to_lua({"type": "activate", "value": section.value, "action": "remove"}))
        self.assertEqual(self.commands(), [])
        self.assertIn("Part 2", [row.title for row in self.rows()])

    def test_reopening_preserves_manual_fold_and_focuses_visible_current_parent(self):
        self.controller.open()
        video = self.row("Video 1001").value
        self.activate(video)
        self.g.Menu.close(self.g.Menu)
        self.assertTrue(self.controller.open())
        self.assertNotIn("Part 2", [row.title for row in self.rows()])
        self.assertEqual(self.selected_value(), video)

    def test_large_list_open_scrolls_to_current_item_after_selection(self):
        items = [self.fixture.item(aid=1000 + index, cid=2000 + index, order=index) for index in range(1, 1002)]
        native = self.fixture.native(items, current=items[-1]["itemId"])
        self.t.props[MANIFEST] = self.to_lua(self.fixture.manifest(items))
        self.t.props.playlist = self.to_lua(native)
        self.assertTrue(self.controller.open())
        self.assertEqual(self.t.menu.current.selected_index, 1001)
        self.assertEqual(self.t.menu.lastScrolledIndex, 1001)
        self.assertTrue(self.t.menu.scrollImmediate)

    def test_pending_collection_is_labelled_without_claiming_its_partial_count_is_complete(self):
        manifest = self.fixture.manifest(self.items)
        manifest["metadataPending"] = True
        self.t.props[MANIFEST] = self.to_lua(manifest)
        self.controller.open()
        self.assertIn("合集加载中", self.t.menu.data.title)
        self.assertNotIn("(1/", self.t.menu.data.title)
        self.assertIn("完整合集正在后台加载", self.t.menu.data.footnote)
        self.assertEqual(self.t.props[STATUS].metadataStatus, "pending")
        manifest["revision"] = 2
        manifest["metadataPending"] = False
        self.t.props[MANIFEST] = self.to_lua(manifest)
        self.t.fire(MANIFEST)
        self.assertNotIn("加载中", self.t.menu.data.title)
        self.assertIn("(1/2)", self.t.menu.data.title)
        self.assertEqual(self.t.props[STATUS].metadataStatus, "ready")

    def test_failed_background_metadata_stays_playable_and_is_not_shown_as_complete(self):
        self.controller.open()
        self.t.props[METADATA_STATE] = self.to_lua({"playlistId": self.fixture.manifest(self.items)["playlistId"],
                                                   "revision": 2, "status": "failed"})
        self.t.fire(METADATA_STATE)
        self.assertIn("合集加载失败", self.t.menu.data.title)
        self.assertIn("当前视频可继续播放", self.t.menu.data.footnote)
        self.assertEqual(self.t.props[STATUS].metadataStatus, "failed")
        self.activate(self.row("Part 2").value)
        self.assertEqual(self.commands(), [["set", "playlist-pos", "1"]])


if __name__ == "__main__":
    unittest.main()
