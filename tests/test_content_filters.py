from __future__ import annotations

import unittest

from content_filters import compile_filters, passes_filters


class ContentFilterTests(unittest.TestCase):
    def test_exclude_keywords_block_title_or_summary_matches(self) -> None:
        spec = compile_filters([{"exclude_keywords": ["sports", "lottery"]}])

        self.assertFalse(
            passes_filters(
                {"title": "High school sports roundup", "summary": ""},
                spec,
            )
        )
        self.assertFalse(
            passes_filters(
                {"title": "Budget hearing", "summary": "Lottery funds debated"},
                spec,
            )
        )

    def test_include_keywords_require_at_least_one_match(self) -> None:
        spec = compile_filters([{"include_keywords": ["housing", "transit"]}])

        self.assertTrue(
            passes_filters(
                {"title": "Council discusses transit routes", "summary": ""},
                spec,
            )
        )
        self.assertFalse(
            passes_filters(
                {"title": "Council honors retirees", "summary": ""},
                spec,
            )
        )

    def test_source_include_overrides_channel_include(self) -> None:
        spec = compile_filters(
            [
                {"include_keywords": ["weather"]},
                {"include_keywords": ["agenda"]},
            ]
        )

        self.assertTrue(passes_filters({"title": "New agenda posted"}, spec))
        self.assertFalse(passes_filters({"title": "Weather update"}, spec))

    def test_excludes_are_additive_across_filter_levels(self) -> None:
        spec = compile_filters(
            [
                {"exclude_keywords": ["sports"]},
                {"exclude_keywords": ["sponsored"]},
            ]
        )

        self.assertFalse(passes_filters({"title": "Sponsored post"}, spec))
        self.assertFalse(passes_filters({"title": "Sports notes"}, spec))
        self.assertTrue(passes_filters({"title": "Transit meeting"}, spec))

    def test_counties_and_meeting_location_are_filterable(self) -> None:
        spec = compile_filters([{"include_keywords": ["greenville"]}])

        self.assertTrue(
            passes_filters(
                {"headline": "Flood Watch", "counties": ["Greenville", "Pickens"]},
                spec,
            )
        )
        self.assertTrue(
            passes_filters(
                {"title": "Board meeting", "where": "Greenville City Hall"},
                spec,
            )
        )

    def test_keyword_mappings_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            compile_filters([{"include_keywords": {"bad": "shape"}}])


if __name__ == "__main__":
    unittest.main()
