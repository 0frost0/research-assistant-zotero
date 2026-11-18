import unittest
from benchmarks.run_ic6_requirements import parse_needs, coverage_select


class RequirementsTests(unittest.TestCase):
    def test_strict_json_no_repair(self):
        self.assertEqual(len(parse_needs('["First needed fact", "Second needed fact"]')), 2)
        for raw in ['[]', '["One needed fact"]', '```json\n["First fact", "Second fact"]\n```',
                    '["duplicate fact", "duplicate fact"]', '[1, 2]']:
            with self.assertRaises((ValueError, TypeError)):
                parse_needs(raw)

    def test_selection_covers_complementary_need(self):
        ids = list('abcdefghij')
        original = [10-i for i in range(10)]
        needs = [[10-i for i in range(10)], [0]*9+[10]]
        selection = coverage_select(ids, original, needs)
        self.assertEqual(len(selection), 6)
        self.assertEqual(len(set(selection)), 6)
        self.assertIn('j', selection)
        self.assertEqual(selection, coverage_select(ids, original, needs))

    def test_no_padding_or_duplicate_candidates(self):
        with self.assertRaises(ValueError):
            coverage_select(['a']*10, [1]*10, [[1]*10])
        with self.assertRaises(ValueError):
            coverage_select(['a','b'], [1,2], [[1,2]])
        with self.assertRaises(ValueError):
            coverage_select(list('abcdefghij'), [1]*10, [[1]])
