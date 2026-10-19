import subprocess
import sys
import tempfile
import unittest

from mediaflow.browser_lease import BrowserLease, BrowserBusy


class BrowserLeaseTests(unittest.TestCase):
    def test_loopback_aliases_share_lock_but_other_browser_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            with BrowserLease('http://localhost:9600', directory):
                with self.assertRaises(BrowserBusy):
                    BrowserLease('http://127.0.0.1:9600/', directory).acquire()
                with BrowserLease('http://127.0.0.1:9601', directory):
                    pass
            with BrowserLease('http://127.0.0.1:9600', directory):
                pass

    def test_process_death_releases_kernel_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            source = "from mediaflow.browser_lease import BrowserLease; import sys,time; lease=BrowserLease('http://localhost:9600',sys.argv[1]).acquire(); print('locked',flush=True); time.sleep(60)"
            child = subprocess.Popen([sys.executable, '-c', source, directory], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'locked')
                with self.assertRaises(BrowserBusy):
                    BrowserLease('http://localhost:9600', directory).acquire()
            finally:
                child.kill()
                child.wait(timeout=10)
                child.stdout.close()
            with BrowserLease('http://localhost:9600', directory):
                pass
