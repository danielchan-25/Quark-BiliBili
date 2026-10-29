"""Regression for a comment placeholder that moves before its component loads."""
import os
import time
import unittest
from unittest.mock import Mock

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

from mediaflow.bilibili import _scroll_to_comment_section


LAZY_PAGE = """<!doctype html><style>body{margin:0}</style>
<div style="height:950px"></div><div id="late-layout"></div>
<div id="commentapp"></div><div style="height:1800px"></div>
<script>
window.commentLoaded = false;
setTimeout(() => {
  document.querySelector('#late-layout').style.height = '700px';
  const component = document.createElement('bili-comments');
  component.style.display = 'block';
  component.attachShadow({mode:'open'}).innerHTML = '<div style="height:150px">Loading</div>';
  document.querySelector('#commentapp').append(component);
  const observer = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.intersectionRatio >= .75)) {
      window.commentLoaded = true;
      observer.disconnect();
    }
  }, {threshold:.75});
  observer.observe(component);
}, 150);
</script>"""


class CommentComponentScrollTests(unittest.TestCase):
    def test_empty_placeholder_is_not_the_component_scroll_target(self):
        page, placeholder, component = Mock(), Mock(), Mock()
        page.locator.side_effect = lambda selector: {
            '#commentapp, bili-comments': Mock(first=placeholder),
            'bili-comments': Mock(first=component),
        }[selector]
        _scroll_to_comment_section(page, reload_once=False)
        component.wait_for.assert_called_once_with(state='visible', timeout=15_000)
        component.scroll_into_view_if_needed.assert_called_once()
        placeholder.scroll_into_view_if_needed.assert_not_called()
        page.reload.assert_not_called()

    def test_component_visibility_failure_respects_shared_deadline(self):
        page, placeholder, component = Mock(), Mock(), Mock()
        page.locator.side_effect = lambda selector: {
            '#commentapp, bili-comments': Mock(first=placeholder),
            'bili-comments': Mock(first=component),
        }[selector]
        component.wait_for.side_effect = PlaywrightTimeoutError('not rendered')
        with self.assertRaises(PlaywrightTimeoutError):
            _scroll_to_comment_section(page, reload_once=False, deadline=time.monotonic() + 1)
        self.assertLessEqual(component.wait_for.call_args.kwargs['timeout'], 1000)
        component.scroll_into_view_if_needed.assert_not_called()
        page.reload.assert_not_called()

    @unittest.skipUnless(os.getenv('MEDIAFLOW_LIVE_TEST') == '1', 'opt-in isolated Chromium fixture')
    def test_delayed_component_loads_after_placeholder_layout_shift(self):
        with sync_playwright() as playwright:
            options = dict(headless=True)
            if os.getenv('MEDIAFLOW_TEST_BROWSER'):
                options['executable_path'] = os.environ['MEDIAFLOW_TEST_BROWSER']
            # Only the isolated test browser; external production profiles are untouched.
            if os.getenv('MEDIAFLOW_TEST_NO_SANDBOX') == '1':
                options['args'] = ['--no-sandbox']
            browser = playwright.chromium.launch(**options)
            try:
                page = browser.new_page(viewport=dict(width=1042, height=789))
                page.set_content(LAZY_PAGE)
                _scroll_to_comment_section(page, reload_once=False, deadline=time.monotonic() + 5)
                page.wait_for_function('window.commentLoaded === true', timeout=1000)
            finally:
                browser.close()
