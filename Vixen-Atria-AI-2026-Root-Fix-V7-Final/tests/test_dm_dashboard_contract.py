import re
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DMDashboardContractTests(unittest.TestCase):
    def test_dashboard_ids_are_unique(self):
        html = (ROOT / 'dashboard' / 'index.html').read_text(encoding='utf-8')
        ids = re.findall(r'\bid=["\']([^"\']+)["\']', html)
        duplicates = {key: count for key, count in Counter(ids).items() if count > 1}
        self.assertEqual(duplicates, {})

    def test_dm_center_and_dm_all_controls_exist(self):
        html = (ROOT / 'dashboard' / 'index.html').read_text(encoding='utf-8')
        for element_id in (
            'dmGuild', 'dmTemplate', 'dmTemplateCategoryFilter', 'dmTemplateCategory', 'dmPreviewButton', 'dmSendButton',
            'dmAllGuild', 'dmAllMode', 'dmAllRoles', 'dmAllExcludeRoles', 'dmAllSelectedIds',
            'dmAllPreviewButton', 'dmAllStart', 'dmJobs',
        ):
            self.assertIn(f'id="{element_id}"', html)

    def test_dm_api_routes_are_registered(self):
        server = (ROOT / 'bot' / 'web' / 'server.py').read_text(encoding='utf-8')
        for route in (
            '/api/dm/center', '/api/dm/members', '/api/dm/roles', '/api/dm/templates', '/api/dm/preview', '/api/dm/send',
            '/api/dm/history', '/api/dm/bulk/preview', '/api/dm/bulk/start', '/api/dm/bulk/jobs',
            '/api/dm/bulk/status', '/api/dm/bulk/control',
        ):
            self.assertIn(route, server)


if __name__ == '__main__':
    unittest.main()
