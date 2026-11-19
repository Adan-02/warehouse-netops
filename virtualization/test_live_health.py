"""Historical acceptance must not turn failed or absent live probes green."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
import collect_status


class StatusTests(unittest.TestCase):
    def test_legacy_acceptance_never_overrides_live_failure(self):
        acceptance=collect_status.summarize_acceptance({'old':{'passed':True,'observed_at':'2020-01-01T00:00:00Z'}})
        self.assertEqual(acceptance['state'],'passed')
        self.assertFalse(acceptance['is_live_health'])
        self.assertEqual(collect_status.combined_state(acceptance,{'state':'unhealthy'}),'unhealthy')
        self.assertEqual(collect_status.combined_state(acceptance,{'state':'unknown'}),'unknown')
        self.assertEqual(collect_status.combined_state(acceptance,{'state':'healthy'}),'healthy')
        self.assertEqual(collect_status.combined_state(acceptance,{'state':'healthy'},False),'unhealthy')

    def test_empty_acceptance_is_incomplete(self):
        self.assertEqual(collect_status.summarize_acceptance({})['state'],'incomplete')


if __name__=='__main__': unittest.main()
