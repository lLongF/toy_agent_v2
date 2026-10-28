"""Windows DPAPI roundtrip and token lifecycle; all data is temporary."""
import os
import tempfile
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import patch

from local_credentials import read_token, save_token
from toy_management import create_toy, update_toy, file_hash
from toy_profiles import apply_toy_environment


@skipUnless(os.name == "nt", "DPAPI is Windows-only")
class LocalCredentialTests(TestCase):
    def test_encryption_roundtrip_no_plaintext(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reference = save_token("1234", root)
            self.assertEqual(read_token(reference, root), "1234")
            self.assertNotIn(b"1234", next((root / '.runtime/credentials').glob('*.dpapi')).read_bytes())
            with self.assertRaises(ValueError):
                read_token('local:../escape', root)

    def test_toy_token_override_and_empty_edit_retains(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            toy = create_toy('Test', root=root, token='local-test-ota-token')
            with patch.dict(os.environ, {'TOY_AGENT_TOKEN': 'wrong-global'}):
                self.assertEqual(toy.resolve_token(), 'local-test-ota-token')
                path = toy.root / 'toy.json'
                updated = update_toy(toy, expected_hash=file_hash(path), name='Renamed', token='', root=root)
                self.assertEqual(updated.resolve_token(), 'local-test-ota-token')
                # 实际进入协议层的OTA请求必须携带解密后的原Token，而不是local引用。
                import asyncio
                from types import SimpleNamespace
                from collection import MessageRouter, TransportClient
                from config import RuntimeState
                cfg = SimpleNamespace(token=updated.resolve_token(), device_mac='test-device', client_id='test',
                                      ota_url='http://127.0.0.1/ota', ota_version=0, board_ip='127.0.0.1',
                                      board_type='test', firmware_version='1.0', application_name='test')
                state = RuntimeState()
                transport = TransportClient(cfg, state, MessageRouter(state))
                response = SimpleNamespace(ok=True, status_code=200, json=lambda: {'websocket': {'url': 'ws://localhost'}})
                with patch('collection.requests.post', return_value=response) as post:
                    asyncio.run(transport.send_ota_request())
                self.assertEqual(post.call_args.kwargs['json']['token'], 'local-test-ota-token')
                for file in root.rglob('*'):
                    if file.is_file():
                        self.assertNotIn(b'local-test-ota-token', file.read_bytes())
