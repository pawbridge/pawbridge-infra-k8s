"""Offline contracts for the optional two-Community chat test topology."""
from pathlib import Path
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import yaml
import member_chat_test

ROOT = Path(__file__).resolve().parents[2]


class MemberChatContractTest(unittest.TestCase):
    def test_local_compose_keeps_an_isolated_chat_namespace(self):
        compose = yaml.safe_load((ROOT / 'environments/dev/compose/compose.yaml').read_text())
        community = compose['services']['community-service']['environment']
        self.assertEqual('true', community['MEMBER_CHAT_ENABLED'])
        self.assertEqual('local-dev', community['MEMBER_CHAT_NAMESPACE'])
        self.assertNotIn('*', community['MEMBER_CHAT_ALLOWED_ORIGINS'])
        production = yaml.safe_load((ROOT / 'environments/prod/values/community-service.yaml').read_text())
        self.assertNotEqual(community['MEMBER_CHAT_NAMESPACE'],
                            production['env']['MEMBER_CHAT_NAMESPACE'])

    def test_cross_app_overlay_reuses_existing_network_and_database(self):
        overlay = yaml.safe_load((ROOT / 'environments/dev/compose/compose.chat-test.yaml').read_text())
        peer = overlay['services']['community-chat-peer']
        gateway = overlay['services']['chat-peer-gateway']
        self.assertEqual('community-service', peer['extends']['service'])
        self.assertEqual(['chat-test'], peer['profiles'])
        self.assertEqual('http://community-chat-peer:8082', gateway['environment']['COMMUNITY_SERVICE_URL'])
        self.assertEqual('ws://community-chat-peer:8082', gateway['environment']['COMMUNITY_CHAT_WEBSOCKET_URL'])
        self.assertEqual(['127.0.0.1:28180:8080'], gateway['ports'])
        self.assertNotIn('volumes', overlay)
        self.assertNotIn('networks', overlay)

    def test_up_rejects_running_app_without_task_ownership_before_recreation(self):
        with TemporaryDirectory() as directory, \
                patch.object(member_chat_test, 'guard'), \
                patch.object(member_chat_test, 'db_sql', return_value='1'), \
                patch.object(member_chat_test, 'chat_cli') as compose, \
                patch.object(member_chat_test, 'cli', return_value=SimpleNamespace(stdout='user-service\n')), \
                patch.object(member_chat_test, 'write_private') as write:
            with self.assertRaises(ValueError):
                member_chat_test.up(Path(directory), Path('/synthetic/backend'))
            compose.assert_called_once_with(Path(directory), Path('/synthetic/backend'), 'config', '--quiet')
            write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
