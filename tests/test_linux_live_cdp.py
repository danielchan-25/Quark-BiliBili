"""Opt-in real Chromium smoke test; local temporary page only, no Bilibili traffic."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import build_opener, ProxyHandler

from playwright.sync_api import sync_playwright
from mediaflow.bilibili import BilibiliClient


class LocalPage(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'<!doctype html><html><body><h1 id="ready">local CDP ready</h1></body></html>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


@unittest.skipUnless(sys.platform == 'linux' and os.getenv('MEDIAFLOW_LIVE_TEST') == '1', 'opt-in isolated Linux Chromium test')
class LinuxLiveCDPTests(unittest.TestCase):
    def test_client_dom_and_detach_preserve_external_browser(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = root / 'profile'
            with sync_playwright() as playwright:
                browser_binary = playwright.chromium.executable_path
            command = [browser_binary, '--headless=new', '--remote-debugging-address=127.0.0.1',
                       '--remote-debugging-port=0', '--user-data-dir=' + str(profile),
                       '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
                       '--disable-component-update', '--disable-sync', 'about:blank']
            # A CI fixture exception only. Production installation never adds this flag.
            if os.getenv('MEDIAFLOW_TEST_NO_SANDBOX') == '1':
                command.insert(1, '--no-sandbox')
            with (root / 'chrome.log').open('wb') as log:
                process = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True)
                server = ThreadingHTTPServer(('127.0.0.1', 0), LocalPage)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    deadline = time.monotonic() + 30
                    active_port = profile / 'DevToolsActivePort'
                    while not active_port.exists() and time.monotonic() < deadline:
                        if process.poll() is not None:
                            self.fail('Isolated Chromium exited before its CDP endpoint was ready')
                        time.sleep(0.1)
                    self.assertTrue(active_port.exists(), 'Isolated Chromium CDP did not become ready')
                    port = int(active_port.read_text().splitlines()[0])
                    endpoint = f'http://127.0.0.1:{port}'
                    opener = build_opener(ProxyHandler({}))
                    with opener.open(endpoint + '/json/version', timeout=5) as response:
                        before = json.load(response)['webSocketDebuggerUrl']
                    with patch('mediaflow.browser_lease.ROOT', root / 'project'), patch.dict(os.environ, {'MEDIAFLOW_LOG_DIR': str(root / 'logs')}):
                        with BilibiliClient(endpoint) as client:
                            page = client.page()
                            try:
                                page.goto(f'http://127.0.0.1:{server.server_port}/', wait_until='domcontentloaded', timeout=10_000)
                                self.assertEqual(page.locator('#ready').inner_text(), 'local CDP ready')
                                self.assertEqual(page.locator('html').evaluate('element => element.tagName'), 'HTML')
                            finally:
                                page.close()
                    self.assertIsNone(process.poll(), 'Detaching the project must not close the external browser')
                    with opener.open(endpoint + '/json/version', timeout=5) as response:
                        self.assertEqual(json.load(response)['webSocketDebuggerUrl'], before)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=5)
