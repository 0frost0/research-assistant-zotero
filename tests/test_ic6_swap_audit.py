import unittest

from benchmarks.audit_ic6_swap import opportunity


class SwapAuditTests(unittest.TestCase):
    def test_redundant_slot_allows_preserving_gain(self):
        r=opportunity([{0},{0},set(),set(),set(),set(),{1}],2)
        self.assertEqual(r['baseline_ic'],.5)
        self.assertEqual(r['oracle_single_swap_ic_diagnostic_only'],1)
        self.assertTrue(r['gain_without_losing_old_gold_opportunity'])

    def test_unique_facts_replacement_may_gain_without_preservation(self):
        r=opportunity([{i} for i in range(6)]+[{6,7}],8)
        self.assertTrue(r['improvement_opportunity'])
        self.assertFalse(r['gain_without_losing_old_gold_opportunity'])
        self.assertEqual(r['best_covered_diagnostic_only'],7)

    def test_zero_opportunity_no_challengers_and_null(self):
        self.assertFalse(opportunity([{0}]*6,1)['improvement_opportunity'])
        self.assertEqual(opportunity([{0}]*7,1)['best_covered_diagnostic_only'],1)
        self.assertIsNone(opportunity([set()]*7,0)['baseline_ic'])
        with self.assertRaises(ValueError):
            opportunity([{1}]*7,1)
