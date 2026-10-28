"""TLS trust and connection diagnostics; no external service requests."""
import ssl
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from collection import MessageRouter, TransportClient
from config import RuntimeState


class TLSConnectionTests(unittest.IsolatedAsyncioTestCase):
    def transport(self):
        state = RuntimeState()
        cfg = SimpleNamespace(device_mac='test-device', client_id='test-client')
        return TransportClient(cfg, state, MessageRouter(state))

    async def test_wss_adds_bundle_without_disabling_verification(self):
        transport = self.transport()
        context = ssl.create_default_context()
        with patch('collection.ssl.create_default_context', return_value=context), \
             patch('collection.websockets.connect', new_callable=AsyncMock) as connect:
            self.assertTrue(await transport.connect_websocket('wss://example.invalid/ws', 'test-token'))
        actual = connect.call_args.kwargs['ssl']
        self.assertIs(actual, context)
        self.assertTrue(actual.check_hostname)
        self.assertEqual(actual.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreater(actual.cert_store_stats()['x509_ca'], 0)

    async def test_ws_does_not_receive_ssl_options(self):
        with patch('collection.websockets.connect', new_callable=AsyncMock) as connect:
            self.assertTrue(await self.transport().connect_websocket('ws://localhost/ws', ''))
        self.assertNotIn('ssl', connect.call_args.kwargs)

    async def test_connection_error_is_available_and_redacted(self):
        transport = self.transport()
        with patch('collection.websockets.connect', new_callable=AsyncMock,
                   side_effect=ssl.SSLCertVerificationError('test-private-token')):
            self.assertFalse(await transport.connect_websocket('wss://example.invalid/ws', 'test-private-token'))
        self.assertIn('SSLCertVerificationError', transport.last_connect_error)
        self.assertNotIn('test-private-token', transport.last_connect_error)
        self.assertIn('[REDACTED]', transport.last_connect_error)
