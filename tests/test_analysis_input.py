import unittest

from scripts.analysis_input import parse_analysis_input, validate_analysis_input


def manifest():
    return {
        "schema_version": 3,
        "run_id": "run-1",
        "packages": {
            "material_ranking": {
                "path": "facts/material-ranking.json",
                "record_count": 10,
                "material_ids": ["101", "202"],
            },
            "audience": {
                "path": "facts/audience.json",
                "record_count": 10,
                "material_ids": ["101", "202"],
            },
            "script": {
                "path": "facts/script.json",
                "record_count": 2,
                "material_ids": ["101", "202"],
            },
            "highlight_frames": {
                "path": "facts/highlight-frames.json",
                "record_count": 2,
                "material_ids": ["101", "202"],
            },
        },
        "brand_classification": {
            "candidate_brands": ["品牌A", "品牌B"],
            "unknown_count": 1,
        },
    }


class AnalysisInputTests(unittest.TestCase):
    def test_natural_language_input_uses_ready_run_and_all_fact_modules(self):
        spec = parse_analysis_input(
            "比较素材表现和人群差异",
            {"status": "ready_for_analysis", "run_id": "run-1"},
            manifest(),
        )

        self.assertEqual(spec.run_id, "run-1")
        self.assertEqual(
            spec.sections,
            ("material_ranking", "audience", "script", "highlight_frames"),
        )
        self.assertEqual(spec.business_question, "比较素材表现和人群差异")
        self.assertEqual(spec.business_question_source, "user")
        self.assertTrue(spec.user_confirmed)
        self.assertTrue(spec.analysis_id)

    def test_confirmed_default_question_is_preserved_as_confirmed_default(self):
        spec = parse_analysis_input({
            "run_id": "run-1",
            "sections": ["audience"],
            "business_question": "系统默认商业问题",
            "business_question_source": "user_confirmed_default",
            "user_confirmed": True,
        }, {"status": "ready_for_analysis", "run_id": "run-1"}, manifest())

        self.assertEqual(spec.business_question_source, "user_confirmed_default")
        self.assertTrue(spec.user_confirmed)
        self.assertEqual(spec.to_dict()["business_question"], "系统默认商业问题")

    def test_structured_input_maps_brand_and_material_filters_to_analysis_spec(self):
        spec = parse_analysis_input({
            "run_id": "run-1",
            "sections": ["material_ranking", "highlight_frames"],
            "brand_filter": ["品牌A", "未知品牌"],
            "material_filter": {"rank_range": [1, 4], "material_ids": ["101"]},
            "metric_focus": ["exposure", "click"],
            "business_question": "比较两个品牌的素材表现",
        }, {"status": "ready_for_analysis", "run_id": "run-1"}, manifest())

        self.assertEqual(spec.filters["brand_filter"], ("品牌A", "未知品牌"))
        self.assertEqual(spec.filters["material_ids"], ("101",))
        self.assertEqual(spec.filters["rank_range"], (1, 4))

    def test_empty_question_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "business_question"):
            parse_analysis_input(
                {"run_id": "run-1", "sections": ["audience"], "business_question": " "},
                {"status": "ready_for_analysis", "run_id": "run-1"},
                manifest(),
            )

        with self.assertRaisesRegex(ValueError, "business_question"):
            parse_analysis_input(
                " ",
                {"status": "ready_for_analysis", "run_id": "run-1"},
                manifest(),
            )

    def test_default_question_without_explicit_confirmation_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "user_confirmed_default"):
            parse_analysis_input({
                "run_id": "run-1",
                "sections": ["audience"],
                "business_question": "系统默认商业问题",
                "business_question_source": "user_confirmed_default",
            }, {"status": "ready_for_analysis", "run_id": "run-1"}, manifest())

    def test_validation_rejects_run_status_and_scope_errors_together(self):
        spec = parse_analysis_input({
            "run_id": "other-run",
            "sections": ["material_ranking"],
            "brand_filter": ["未指定品牌"],
            "material_filter": {"rank_range": [1, 10], "material_ids": ["999"]},
            "metric_focus": ["not-a-metric"],
            "business_question": "比较素材",
        }, {"status": "partial", "run_id": "run-1"}, manifest())

        errors = validate_analysis_input(spec, {"status": "partial", "run_id": "run-1"}, manifest())

        self.assertTrue(any("ready_for_analysis" in error for error in errors))
        self.assertTrue(any("run_id" in error for error in errors))
        self.assertTrue(any("brand_filter" in error for error in errors))
        self.assertTrue(any("material_ids" in error for error in errors))
        self.assertTrue(any("metric_focus" in error for error in errors))

    def test_validation_rejects_unknown_module_and_out_of_range_rank(self):
        with self.assertRaisesRegex(ValueError, "unsupported section"):
            parse_analysis_input({
                "run_id": "run-1",
                "sections": ["unknown"],
                "business_question": "比较素材",
            }, {"status": "ready_for_analysis", "run_id": "run-1"}, manifest())

        spec = parse_analysis_input({
            "run_id": "run-1",
            "sections": ["material_ranking"],
            "material_filter": {"rank_range": [11, 20]},
            "business_question": "比较素材",
        }, {"status": "ready_for_analysis", "run_id": "run-1"}, manifest())
        errors = validate_analysis_input(spec, {"status": "ready_for_analysis", "run_id": "run-1"}, manifest())
        self.assertTrue(any("rank_range" in error for error in errors))


if __name__ == "__main__":
    unittest.main()

class ScriptWorkbenchDefaultTests(unittest.TestCase):
    def test_default_omits_missing_highlights_but_explicit_request_is_rejected(self):
        facts = manifest(); del facts['packages']['highlight_frames']
        status = {'status': 'ready_for_analysis', 'run_id': 'run-1'}
        spec = parse_analysis_input('分析脚本开头', status, facts)
        self.assertEqual(spec.sections, ('material_ranking', 'audience', 'script'))
        self.assertEqual(validate_analysis_input(spec, status, facts), [])
        explicit = parse_analysis_input({'business_question': '分析画面', 'sections': ['highlight_frames']}, status, facts)
        self.assertTrue(any('highlight_frames' in str(e) for e in validate_analysis_input(explicit, status, facts)))
