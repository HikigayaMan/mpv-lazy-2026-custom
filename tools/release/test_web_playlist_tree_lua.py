"""Inline collection/video/part hierarchy using the production pure Lua builder.

Rendering order follows the public manifest; playback bindings follow MPV's
current native entries. The tests deliberately reorder those two independently.
"""
from __future__ import annotations

from pathlib import Path
import unittest

try:
    from lupa.lua51 import LuaRuntime
except ImportError:
    LuaRuntime = None

SOURCE = Path(__file__).resolve().parents[2] / "portable_config/scripts/uosc/lib/web_playlist_tree.lua"
PLAYLIST_ID = "playlist-" + "a" * 32


@unittest.skipIf(LuaRuntime is None, "lupa.lua51 is required")
class WebPlaylistTreeLuaTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.builder = self.lua.execute(SOURCE.read_text(encoding="utf-8"))

    def to_lua(self, value):
        return self.lua.table_from(value, recursive=True) if isinstance(value, (dict, list)) else value

    def to_python(self, value):
        if hasattr(value, "items"):
            values = dict(value.items())
            if values and set(values) == set(range(1, len(values) + 1)):
                return [self.to_python(values[index]) for index in range(1, len(values) + 1)]
            return {key: self.to_python(item) for key, item in values.items()}
        return value

    def path(self, item_id):
        return f"http://127.0.0.1:9002/web-playlist/{PLAYLIST_ID}/item/{item_id}"

    def item(self, aid=1001, cid=2001, p=1, part_count=1, order=1, section="section-1", **changes):
        item = {"itemId": f"{aid}:{cid}", "videoId": str(aid), "videoTitle": f"Video {aid}",
                "partTitle": f"Part {p}", "sectionId": section, "sectionTitle": f"Section {section}",
                "p": p, "partCount": part_count, "order": order, "duration": 60 + p,
                "title": f"Video {aid} / Part {p}"}
        item.update(changes)
        return item

    def manifest(self, items):
        return {"playlistId": PLAYLIST_ID, "revision": 1, "collectionTitle": "Complete collection", "items": items}

    def native(self, items, current=None):
        return [{"id": 100 + index, "filename": self.path(item["itemId"]), "title": item["title"],
                 "current": item["itemId"] == current} for index, item in enumerate(items)]

    def build(self, items, native=None, expanded=None):
        value = self.builder.build(self.to_lua(self.manifest(items)), self.to_lua(self.native(items) if native is None else native),
                                   self.to_lua(expanded) if expanded is not None else None)
        return self.to_python(value)

    def rows(self, result):
        return list(result["rows"].values()) if isinstance(result["rows"], dict) else result["rows"]

    def leaf_value(self, result, item_id):
        return next(key for key, binding in result["bindings"].items() if binding.get("itemId") == item_id)

    def parents(self, result, value):
        parents = result["parentKeys"].get(value, [])
        return list(parents.values()) if isinstance(parents, dict) else parents

    def expanded_all(self, result):
        return {key: True for key in result["groups"]}

    def test_single_section_has_no_redundant_section_layer(self):
        items = [self.item(), self.item(1002, 2002, order=2)]
        result = self.build(items)
        self.assertEqual(len(self.rows(result)), 2)
        self.assertEqual(result["videoCount"], 2)
        self.assertEqual(result["partCount"], 2)
        self.assertEqual(result["collectionTitle"], "Complete collection")
        self.assertEqual(len(result["groups"]), 0)
        for row in self.rows(result):
            self.assertEqual(row["depth"], 0)
            self.assertIn(row["value"], result["bindings"])
            self.assertEqual(self.parents(result, row["value"]), [])

    def test_multiple_sections_are_collapsed_groups_in_same_rows_array(self):
        items = [self.item(section="one"), self.item(1002, 2002, order=2, section="two")]
        collapsed = self.build(items)
        self.assertEqual(len(self.rows(collapsed)), 2)
        self.assertEqual(len(collapsed["groups"]), 2)
        self.assertEqual(len(collapsed["bindings"]), 2)
        for row in self.rows(collapsed):
            self.assertIn(row["value"], collapsed["groups"])
            self.assertNotIn(row["value"], collapsed["bindings"])
            self.assertTrue(row["keep_open"])
            self.assertEqual(row["depth"], 0)
            self.assertNotIn("items", row)
        expanded = self.build(items, expanded=self.expanded_all(collapsed))
        self.assertEqual(len(self.rows(expanded)), 4)
        for item in items:
            value = self.leaf_value(expanded, item["itemId"])
            self.assertEqual(len(self.parents(expanded, value)), 1)
            row = next(row for row in self.rows(expanded) if row["value"] == value)
            self.assertEqual(row["depth"], 1)

    def test_video_with_more_than_two_parts_is_one_collapsible_group(self):
        items = [self.item(cid=2000 + p, p=p, part_count=4, order=p) for p in range(1, 5)]
        collapsed = self.build(items)
        self.assertEqual(len(self.rows(collapsed)), 1)
        group = self.rows(collapsed)[0]
        self.assertEqual(group["title"], "Video 1001")
        self.assertTrue(group["keep_open"])
        self.assertIn(group["value"], collapsed["groups"])
        self.assertEqual(len(collapsed["bindings"]), 4)
        expanded = self.build(items, expanded={group["value"]: True})
        self.assertEqual(len(self.rows(expanded)), 5)
        self.assertEqual([row["depth"] for row in self.rows(expanded)], [0, 1, 1, 1, 1])
        self.assertEqual([self.rows(expanded)[p]["title"] for p in range(1, 5)], [f"Part {p}" for p in range(1, 5)])
        self.assertEqual(expanded["videoCount"], 1)
        self.assertEqual(expanded["partCount"], 4)

    def test_collection_section_video_part_parents_are_ordered_outer_to_inner(self):
        items = [self.item(cid=2001, p=1, part_count=3, order=1, section="one"),
                 self.item(cid=2002, p=2, part_count=3, order=2, section="one"),
                 self.item(cid=2003, p=3, part_count=3, order=3, section="one"),
                 self.item(1002, 3001, order=4, section="two")]
        collapsed = self.build(items)
        result = self.build(items, expanded=self.expanded_all(collapsed))
        value = self.leaf_value(result, "1001:2002")
        parents = self.parents(result, value)
        self.assertEqual(len(parents), 2)
        parent_rows = {row["value"]: row for row in self.rows(result) if row["value"] in result["groups"]}
        self.assertEqual(parent_rows[parents[0]]["title"], "Section one")
        self.assertEqual(parent_rows[parents[0]]["depth"], 0)
        self.assertEqual(parent_rows[parents[1]]["title"], "Video 1001")
        self.assertEqual(parent_rows[parents[1]]["depth"], 1)
        leaf = next(row for row in self.rows(result) if row["value"] == value)
        self.assertEqual(leaf["depth"], 2)

    def test_single_part_video_is_direct_leaf_inside_section(self):
        items = [self.item(videoTitle="Single video", partTitle="Same video", section="one"),
                 self.item(1002, 3001, order=2, section="two")]
        collapsed = self.build(items)
        result = self.build(items, expanded=self.expanded_all(collapsed))
        value = self.leaf_value(result, "1001:2001")
        row = next(row for row in self.rows(result) if row["value"] == value)
        self.assertEqual(row["title"], "Single video")
        self.assertEqual(row["depth"], 1)
        self.assertEqual(len(self.parents(result, value)), 1)
        self.assertEqual(len(result["groups"]), 2)

    def test_current_leaf_is_identified_even_when_all_ancestors_are_collapsed(self):
        items = [self.item(cid=2001, p=1, part_count=3, order=1),
                 self.item(cid=2002, p=2, part_count=3, order=2),
                 self.item(cid=2003, p=3, part_count=3, order=3)]
        native = self.native(items, current="1001:2002")
        result = self.build(items, native)
        current = self.leaf_value(result, "1001:2002")
        self.assertEqual(result["currentValue"], current)
        self.assertEqual(len(self.parents(result, current)), 1)
        expanded = self.build(items, native, self.expanded_all(result))
        active = [row["value"] for row in self.rows(expanded) if row.get("active")]
        self.assertIn(current, active)

    def test_native_shuffle_changes_bindings_without_changing_manifest_display_order(self):
        items = [self.item(cid=2000 + p, p=p, part_count=3, order=p) for p in range(1, 4)]
        original = self.native(items)
        shuffled = [original[2], original[0], original[1]]
        collapsed = self.build(items, shuffled)
        result = self.build(items, shuffled, self.expanded_all(collapsed))
        leaf_rows = [row for row in self.rows(result) if row["value"] in result["bindings"]]
        self.assertEqual([result["bindings"][row["value"]]["itemId"] for row in leaf_rows], [item["itemId"] for item in items])
        for index, item in enumerate(items):
            binding = result["bindings"][self.leaf_value(result, item["itemId"])]
            self.assertEqual(binding["nativeId"], original[index]["id"])
            self.assertEqual(binding["filename"], original[index]["filename"])

    def test_part_display_uses_manifest_order_instead_of_array_or_native_order(self):
        part_1 = self.item(cid=2001, p=1, part_count=3, order=3)
        part_2 = self.item(cid=2002, p=2, part_count=3, order=1)
        part_3 = self.item(cid=2003, p=3, part_count=3, order=2)
        items = [part_1, part_3, part_2]
        collapsed = self.build(items)
        result = self.build(items, expanded=self.expanded_all(collapsed))
        leaves = [row for row in self.rows(result) if row["value"] in result["bindings"]]
        self.assertEqual([result["bindings"][row["value"]]["itemId"] for row in leaves], ["1001:2002", "1001:2003", "1001:2001"])

    def test_removed_native_item_is_absent_from_rows_and_bindings(self):
        items = [self.item(cid=2000 + p, p=p, part_count=3, order=p) for p in range(1, 4)]
        native = self.native(items)
        native.pop(1)
        collapsed = self.build(items, native)
        result = self.build(items, native, self.expanded_all(collapsed))
        self.assertFalse(any(binding.get("itemId") == "1001:2002" for binding in result["bindings"].values()))
        self.assertEqual(result["partCount"], 2)
        self.assertEqual(result["videoCount"], 1)
        self.assertEqual(len([row for row in self.rows(result) if row["value"] in result["bindings"]]), 2)

    def test_unknown_appended_native_file_stays_selectable_without_false_collection_parent(self):
        items = [self.item(), self.item(1002, 2002, order=2)]
        native = self.native(items)
        native.append({"id": 901, "filename": "C:/Videos/local.mp4", "title": "Local file", "current": False})
        result = self.build(items, native)
        value, binding = next((key, binding) for key, binding in result["bindings"].items() if binding["filename"] == "C:/Videos/local.mp4")
        self.assertEqual(binding["nativeId"], 901)
        self.assertEqual(self.parents(result, value), [])
        row = next(row for row in self.rows(result) if row["value"] == value)
        self.assertEqual(row["title"], "Local file")
        self.assertEqual(row["depth"], 0)
        self.assertNotIn(value, result["groups"])

    def test_group_row_values_can_never_resolve_to_native_leaf_bindings(self):
        items = [self.item(cid=2001, part_count=2), self.item(cid=2002, p=2, part_count=2, order=2),
                 self.item(1002, 3001, order=3, section="section-2")]
        collapsed = self.build(items)
        result = self.build(items, expanded=self.expanded_all(collapsed))
        self.assertTrue(set(result["groups"]).isdisjoint(result["bindings"]))
        for row in self.rows(result):
            self.assertIsInstance(row["value"], str)
            if row["value"] in result["groups"]:
                self.assertTrue(row["keep_open"])
                self.assertNotIn("items", row)

    def test_1001_entries_have_no_fixed_500_item_or_two_part_limit(self):
        items = [self.item(aid=1000 + index, cid=2000 + index, order=index) for index in range(1, 1002)]
        result = self.build(items)
        self.assertEqual(result["videoCount"], 1001)
        self.assertEqual(result["partCount"], 1001)
        self.assertEqual(len(result["bindings"]), 1001)
        self.assertEqual(len(self.rows(result)), 1001)
        self.assertEqual(len({row["value"] for row in self.rows(result)}), 1001)
        last = self.leaf_value(result, items[-1]["itemId"])
        self.assertEqual(result["bindings"][last]["nativeId"], 1100)

    def test_tree_presentation_does_not_mutate_native_playlist_or_manifest(self):
        items = [self.item(cid=2001, part_count=2), self.item(cid=2002, p=2, part_count=2, order=2)]
        native = self.native(items)
        manifest_lua, native_lua = self.to_lua(self.manifest(items)), self.to_lua(native)
        self.builder.build(manifest_lua, native_lua, None)
        self.assertEqual(self.to_python(manifest_lua), self.manifest(items))
        self.assertEqual(self.to_python(native_lua), native)

    def test_empty_native_playlist_does_not_leave_metadata_only_rows(self):
        result = self.build([self.item(), self.item(1002, 2002, order=2)], native=[])
        self.assertEqual(len(self.rows(result)), 0)
        self.assertEqual(len(result["bindings"]), 0)
        self.assertEqual(len(result["groups"]), 0)
        self.assertEqual(result["partCount"], 0)
        self.assertEqual(result["videoCount"], 0)
        self.assertIsNone(result.get("currentValue"))

    def test_activation_rescans_native_entry_id_after_shuffle(self):
        items = [self.item(), self.item(1002, 2002, order=2), self.item(1003, 2003, order=3)]
        native = self.native(items)
        result = self.build(items, native)
        binding = result["bindings"][self.leaf_value(result, "1003:2003")]
        self.assertEqual(self.builder.resolve_index(self.to_lua(binding), self.to_lua([native[2], native[0], native[1]])), 0)

    def test_activation_of_removed_entry_does_not_play_the_new_occupant_of_its_old_index(self):
        items = [self.item(), self.item(1002, 2002, order=2), self.item(1003, 2003, order=3)]
        native = self.native(items)
        result = self.build(items, native)
        binding = result["bindings"][self.leaf_value(result, "1002:2002")]
        self.assertIsNone(self.builder.resolve_index(self.to_lua(binding), self.to_lua([native[0], native[2]])))

    def test_replacement_with_same_url_and_new_native_id_cannot_receive_stale_activation(self):
        items = [self.item(), self.item(1002, 2002, order=2)]
        native = self.native(items)
        result = self.build(items, native)
        binding = result["bindings"][self.leaf_value(result, "1002:2002")]
        replacement = {**native[1], "id": 901}
        self.assertIsNone(self.builder.resolve_index(self.to_lua(binding), self.to_lua([native[0], replacement])))

    def test_activation_without_native_id_rescans_unique_filename(self):
        items = [self.item(), self.item(1002, 2002, order=2)]
        native = self.native(items)
        for entry in native:
            del entry["id"]
        result = self.build(items, native)
        binding = result["bindings"][self.leaf_value(result, "1002:2002")]
        self.assertEqual(self.builder.resolve_index(self.to_lua(binding), self.to_lua([native[1], native[0]])), 0)


if __name__ == "__main__":
    unittest.main()
