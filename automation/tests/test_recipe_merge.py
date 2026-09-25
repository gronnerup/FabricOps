"""Overlay merging, and the permanent `merge_type` compatibility."""

import unittest

from fabricops.recipe.merge import merge


class MappingTests(unittest.TestCase):
    def test_deep_merge_child_wins(self):
        self.assertEqual(
            merge({"a": {"x": 1, "y": 2}}, {"a": {"y": 3, "z": 4}}),
            {"a": {"x": 1, "y": 3, "z": 4}},
        )

    def test_scalar_child_wins(self):
        self.assertEqual(merge({"a": 1}, {"a": 2}), {"a": 2})

    def test_new_keys_are_added(self):
        self.assertEqual(merge({"a": 1}, {"b": 2}), {"a": 1, "b": 2})


class StrategyTests(unittest.TestCase):
    def test_replace_drops_the_base_node(self):
        self.assertEqual(merge({"a": {"x": 1}}, {"a": {"$merge": "replace", "y": 2}}), {"a": {"y": 2}})

    def test_keep_protects_the_base_value(self):
        self.assertEqual(merge({"a": {"x": 1}}, {"a": {"$merge": "keep", "x": 9}}), {"a": {"x": 1}})

    def test_strategy_is_inherited_by_children(self):
        merged = merge({"a": {"b": {"c": 1}}}, {"a": {"$merge": "keep", "b": {"c": 2}}})
        self.assertEqual(merged["a"]["b"]["c"], 1)

    def test_strategy_keys_never_reach_the_model(self):
        merged = merge({"a": {"x": 1}}, {"a": {"$merge": "replace", "y": 2}})
        self.assertNotIn("$merge", merged["a"])


class ListTests(unittest.TestCase):
    def test_objects_merge_by_name(self):
        base = {"items": [{"name": "Curated", "type": "Lakehouse"}, {"name": "Landing", "type": "Lakehouse"}]}
        overlay = {"items": [{"name": "Curated", "skip_creation": True}]}
        merged = merge(base, overlay)["items"]
        self.assertEqual(merged[0], {"name": "Curated", "type": "Lakehouse", "skip_creation": True})
        self.assertEqual(merged[1]["name"], "Landing")

    def test_new_objects_are_appended(self):
        merged = merge({"items": [{"name": "A"}]}, {"items": [{"name": "B"}]})["items"]
        self.assertEqual([item["name"] for item in merged], ["A", "B"])

    def test_legacy_item_name_identity(self):
        merged = merge({"items": [{"item_name": "Curated", "type": "Lakehouse"}]},
                       {"items": [{"item_name": "Curated", "skip_item_creation": True}]})["items"]
        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0]["skip_item_creation"])

    def test_scalar_lists_union_order_stable(self):
        merged = merge({"tags": ["A", "B"]}, {"tags": ["B", "C"]})["tags"]
        self.assertEqual(merged, ["A", "B", "C"])

    def test_replace_a_list(self):
        merged = merge({"tags": ["A", "B"]}, {"tags": {"$merge": "replace"}})
        self.assertEqual(merged["tags"], {})  # mapping form replaces the list wholesale

    def test_append_strategy_on_objects(self):
        base = {"items": [{"name": "A"}]}
        overlay = {"items": [{"name": "A"}, {"name": "B"}], "$merge": "append"}
        merged = merge(base, overlay)["items"]
        self.assertEqual([item["name"] for item in merged], ["A", "B"])


class LegacyMergeTypeTests(unittest.TestCase):
    def test_merge_type_0_keeps_the_parent(self):
        self.assertEqual(merge({"a": {"x": 1}}, {"a": {"merge_type": 0, "x": 9}}), {"a": {"x": 1}})

    def test_merge_type_1_replaces(self):
        self.assertEqual(merge({"a": {"x": 1, "y": 2}}, {"a": {"merge_type": 1, "x": 9}}), {"a": {"x": 9}})

    def test_merge_type_2_merges_lists_by_identity(self):
        base = {"items": [{"item_name": "Curated", "type": "Lakehouse"}]}
        overlay = {"merge_type": 2, "items": [{"item_name": "Curated", "skip_item_creation": True}]}
        merged = merge(base, overlay)["items"]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["type"], "Lakehouse")

    def test_merge_type_never_reaches_the_model(self):
        merged = merge({"a": 1}, {"merge_type": 2, "b": 2})
        self.assertNotIn("merge_type", merged)


if __name__ == "__main__":
    unittest.main()


class RoleCaseTests(unittest.TestCase):
    """`Admin` and `admin` are one role, not two."""

    def merge(self, defaults, layer):
        from fabricops.engine.plan import _merged_permissions

        return _merged_permissions(defaults, layer)

    def test_casing_does_not_split_a_role(self):
        merged = self.merge(
            {"Admin": [{"type": "Group", "id": "g1"}]},
            {"admin": [{"type": "Group", "id": "g1"}]},
        )
        self.assertEqual(list(merged), ["admin"])
        self.assertEqual(len(merged["admin"]), 1)

    def test_distinct_principals_still_both_land(self):
        merged = self.merge(
            {"Admin": [{"type": "Group", "id": "g1"}]},
            {"admin": [{"type": "WorkspaceIdentity", "name": "Orchestrate"}]},
        )
        self.assertEqual(len(merged["admin"]), 2)

    def test_different_roles_stay_separate(self):
        merged = self.merge(
            {"Admin": [{"type": "Group", "id": "g1"}]},
            {"Member": [{"type": "Group", "id": "g2"}]},
        )
        self.assertEqual(sorted(merged), ["admin", "member"])
