from copy import deepcopy
import unittest
from scripts.analysis_result import validate_analysis_result


class FieldEvidenceTests(unittest.TestCase):
    def setup_evidence(self):
        ref = 'facts/script.json#/records/0'
        index = {ref: {'material_id': '1', 'script_text': '今天分享真实口播。', 'script_segments': [],
            'metrics': {'views': 0}, 'data_availability': {
                'script_text': {'available': True, 'missing_reason': ''},
                'script_segments': {'available': False, 'missing_reason': 'source_empty'}}}}
        finding = {'claim': '正文以今天分享开头。', 'type': 'fact', 'confidence': 'high',
            'fact_refs': [ref], 'field_refs': {ref: ['script_text']},
            'quotes': [{'fact_ref': ref, 'field': 'script_text', 'text': '今天分享'}]}
        return ref, index, {'section': 'script', 'findings': [finding]}

    def test_readable_script_with_empty_segments_passes(self):
        _, index, result = self.setup_evidence()
        self.assertEqual(validate_analysis_result(result, index), [])

    def test_empty_body_cannot_be_bypassed_by_other_field(self):
        ref, index, result = self.setup_evidence()
        index[ref]['script_text'] = ''
        result['findings'][0].update(field_refs={ref: ['material_id']}, quotes=[])
        self.assertTrue(any(e['code'] == 'source_empty' for e in validate_analysis_result(result, index)))

    def test_missing_fields_cross_record_and_edited_quotes_rejected(self):
        ref, index, result = self.setup_evidence()
        for mode in ('empty', 'missing', 'other-record', 'edited', 'outside-field', 'malformed'):
            bad = deepcopy(result); finding = bad['findings'][0]
            if mode == 'empty': finding['field_refs'][ref] = ['script_segments']
            if mode == 'missing': finding['field_refs'][ref] = ['invented']
            if mode == 'other-record': finding['quotes'][0]['fact_ref'] = 'facts/script.json#/records/1'
            if mode == 'edited': finding['quotes'][0]['text'] = '今天给大家分享'
            if mode == 'outside-field': finding['quotes'][0]['field'] = 'material_id'
            if mode == 'malformed': finding['field_refs'] = {ref: 'script_text'}
            with self.subTest(mode=mode): self.assertTrue(validate_analysis_result(bad, index))

    def test_zero_is_available_and_other_guards_still_apply(self):
        ref, index, result = self.setup_evidence()
        finding = result['findings'][0]; finding.update(field_refs={ref: ['metrics.views']}, quotes=[])
        self.assertEqual(validate_analysis_result(result, index), [])
        finding['claim'] = '开头导致转化提升'
        self.assertTrue(any(e['code'] == 'causal_language' for e in validate_analysis_result(result, index)))

    def test_record_level_legacy_stays_conservative(self):
        _, index, result = self.setup_evidence()
        del result['findings'][0]['field_refs']; del result['findings'][0]['quotes']
        self.assertTrue(any(e['code'] == 'source_empty' for e in validate_analysis_result(result, index)))
