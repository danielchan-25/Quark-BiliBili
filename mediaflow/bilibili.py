from __future__ import annotations

import hashlib
import json
import random
import re
import time
from uuid import uuid4
from dataclasses import dataclass
from urllib.parse import quote, urlparse
from urllib.request import build_opener, ProxyHandler
from websocket import WebSocketTimeoutException, create_connection

from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from mediaflow.audit import write_audit_event, audited, audit_step
from mediaflow.config import ROOT
from mediaflow.browser_lease import BrowserLease


_BVID = re.compile(r"/video/(BV[\w]+)")


def _same_comment_target(expected: str, actual: str) -> bool:
    if expected == actual:
        return True
    wanted, current = urlparse(expected), urlparse(actual)
    wanted_bvid, current_bvid = _BVID.search(wanted.path), _BVID.search(current.path)
    # Bilibili may add a trailing slash or tracking query after navigation.
    # Ignore only those changes on the same origin and exact video path.
    return bool(wanted_bvid and current_bvid and wanted_bvid[1] == current_bvid[1]
                and (wanted.scheme, wanted.netloc) == (current.scheme, current.netloc)
                and wanted.path.rstrip('/') == current.path.rstrip('/'))


class SubmissionRejected(RuntimeError):
    """The platform explicitly rejected the comment."""


class SubmissionUncertain(RuntimeError):
    """A submit action occurred, but its outcome is unknown; do not retry."""


class CommentPreparationError(RuntimeError):
    """A bounded preparation attempt failed before typing or submission."""

    def __init__(self, stage: str, attempts: int, error_type: str, reason: str = '') -> None:
        self.stage, self.attempts = stage, attempts
        self.reason, self.error_type = reason, error_type
        self.failures = []
        labels = {'video_visibility': '视频可访问性检查', 'navigation': '视频页面加载', 'dom_probe': '页面自动化读取检查', 'comment_section': '评论区加载', 'selection_budget': '候选检查预算',
                  'comment_permission': '评论权限及主输入框就绪检查', 'editor_resolve': '主输入框定位',
                  'editor_click': '主输入框可见性及点击', 'editor_focus': '主输入框焦点检查'}
        hints = {'comments_disabled_by_author': '作者隐私设置限制评论',
                 'comments_disabled': '页面已禁用主评论框'}
        super().__init__(f"评论准备失败：{labels.get(stage, stage)} (stage={stage}, attempts={attempts}, error={error_type}"
                         + (f", reason={reason}: {hints.get(reason, reason)}" if reason else '') + ')')


class CommentUnavailable(RuntimeError):
    """The page explicitly disables its main comment box; never try a reply box."""

    def __init__(self, reason: str = 'comments_disabled_by_author') -> None:
        self.reason = reason
        super().__init__('作者隐私设置限制，当前账号无法评论' if reason == 'comments_disabled_by_author'
                         else '页面已禁用主评论框，当前账号无法评论')


# Keep readiness, editor selection and submit button tied to the MAIN box.
# #reply-commentbox is a hidden reply editor, not a fallback for posting roots.
_MAIN_COMMENT_BOX = """root => root?.shadowRoot
    ?.querySelector('bili-comments-header-renderer')?.shadowRoot
    ?.querySelector('#commentbox bili-comment-box')"""

_COMMENT_INPUT_STATE = """() => {
  const root = document.querySelector('bili-comments');
  const header = root?.shadowRoot?.querySelector('bili-comments-header-renderer')?.shadowRoot;
  const visible = e => !!e && e.getClientRects().length > 0
    && getComputedStyle(e).visibility !== 'hidden';
  const disabled = header?.querySelector('#disabled-commentbox');
  if (visible(disabled)) return disabled.innerText.includes('UP主隐私设置')
    ? 'disabled_by_author' : 'disabled';
  const editor = header?.querySelector('#commentbox bili-comment-box')?.shadowRoot
    ?.querySelector('bili-comment-rich-textarea')?.shadowRoot
    ?.querySelector('.brt-editor[contenteditable=true]');
  return visible(editor) ? 'ready' : false;
}"""


def _cdp_command(endpoint: str, method: str, params: dict[str, object] | None = None, timeout: float = 2) -> dict:
    """Send one CDP command without attaching Playwright to every open tab."""
    socket = create_connection(endpoint, timeout=timeout, suppress_origin=True)
    try:
        socket.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            socket.settimeout(max(0.001, deadline - time.monotonic()))
            response = json.loads(socket.recv())
            if response.get("id") == 1:
                if "error" in response:
                    raise RuntimeError(f"CDP {method} failed: {response['error']}")
                return response.get("result", {})
    finally:
        socket.close()


def _recover_stalled_profile_info_tab(base_url: str, browser_endpoint: str) -> bool:
    """Replace only an unresponsive, read-only ChromeManager information tab."""
    opener = build_opener(ProxyHandler({}))
    with opener.open(base_url + "/json/list", timeout=5) as response:
        targets = json.load(response)
    for target in targets:
        page_url = target.get("url", "")
        page = urlparse(page_url)
        if (target.get("type") != "page" or "ChromeManager" not in target.get("title", "")
                or page.hostname not in ("127.0.0.1", "localhost", "::1")
                or not page.path.startswith("/profiles/") or not target.get("webSocketDebuggerUrl")):
            continue
        try:
            _cdp_command(target["webSocketDebuggerUrl"], "Page.getFrameTree")
        except WebSocketTimeoutException:
            # Create the replacement first so a failed recovery keeps the old tab.
            created = _cdp_command(browser_endpoint, "Target.createTarget", {"url": page_url, "background": True})
            if not created.get("targetId"):
                raise RuntimeError("CDP did not create the replacement Profile information tab")
            closed = _cdp_command(browser_endpoint, "Target.closeTarget", {"targetId": target["id"]})
            if not closed.get("success"):
                raise RuntimeError("CDP did not close the stalled Profile information tab")
            return True
    return False


def _scroll_to_comment_section(page: Page, *, reload_once: bool = True, deadline: float | None = None) -> None:
    """Wake the comment region, then scroll its rendered component into view."""
    timeout = lambda limit: _remaining_timeout(deadline, limit) if deadline is not None else limit
    selector = "#commentapp, bili-comments"
    root = page.locator(selector).first
    for attempt in range(2 if reload_once else 1):
        if attempt:
            # No editor interaction or submission has happened yet, so a single
            # page reload is safe recovery from a transient component failure.
            page.reload(wait_until="domcontentloaded", timeout=timeout(30_000))
        # Wake lazy loading before spending the readiness budget. Mouse wheel
        # does not require a working locator/utility execution context.
        with audit_step('comment_lazy_load_wakeup', 'browser-flow.jsonl', attempt=attempt + 1):
            page.mouse.wheel(0, 900)
        try:
            root.wait_for(state="attached", timeout=timeout(15_000))
        except PlaywrightTimeoutError:
            if attempt == 0:
                # The component can be deferred until its region approaches the
                # viewport. Wake lazy content, then allow the DOM another chance.
                page.locator("html").evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)", timeout=timeout(2_000))
                try:
                    root.wait_for(state="attached", timeout=timeout(8_000))
                except PlaywrightTimeoutError:
                    if not reload_once:
                        raise
                    continue
            else:
                state = page.evaluate("""() => ({
                  readyState: document.readyState,
                  hasCommentApp: !!document.querySelector('#commentapp'),
                  hasBiliComments: !!document.querySelector('bili-comments'),
                  hasVideoPlayer: !!document.querySelector('.bpx-player-container'),
                  url: location.href
                })""")
                raise RuntimeError(f"Bilibili comment section did not render after one safe reload: {state}")
        try:
            # #commentapp can be an empty, zero-height placeholder. Its layout
            # moves while the video metadata loads, so scrolling that placeholder
            # does not activate the real component's intersection-based loader.
            # Wait for the component's nonempty box before the decisive scroll.
            with audit_step('comment_component_scroll', 'browser-flow.jsonl', attempt=attempt + 1):
                component = page.locator('bili-comments').first
                component.wait_for(state="visible", timeout=timeout(15_000))
                component.scroll_into_view_if_needed(timeout=timeout(10_000))
            return
        except PlaywrightTimeoutError:
            if not reload_once:
                raise
            if attempt:
                raise RuntimeError("Bilibili comment section appeared but could not be brought into view after one safe reload")

    state = page.evaluate("""() => ({
      readyState: document.readyState,
      hasCommentApp: !!document.querySelector('#commentapp'),
      hasBiliComments: !!document.querySelector('bili-comments'),
      hasVideoPlayer: !!document.querySelector('.bpx-player-container'),
      url: location.href
    })""")
    raise RuntimeError(f"Bilibili comment section did not render after wait and lazy-load wakeup: {state}")


def _comment_page_state(page: Page) -> dict:
    """Structural diagnostics only: never capture comment text, DOM or cookies."""
    return page.locator('html').evaluate("""() => {
      const root = document.querySelector('bili-comments');
      const comments = root?.shadowRoot;
      const h = comments?.querySelector('bili-comments-header-renderer');
      const box = h?.shadowRoot?.querySelector('#commentbox bili-comment-box');
      const disabled = h?.shadowRoot?.querySelector('#disabled-commentbox');
      const editor = box?.shadowRoot?.querySelector('bili-comment-rich-textarea')
        ?.shadowRoot?.querySelector('.brt-editor[contenteditable=true]');
      const rect = root?.getBoundingClientRect();
      return {ready_state: document.readyState, document_hidden: document.hidden,
        visibility_state: document.visibilityState, comment_root: !!root,
        comment_shadow: !!comments, comment_header: !!h, header_shadow: !!h?.shadowRoot,
        component_defined: !!customElements.get('bili-comments'),
        header_defined: !!customElements.get('bili-comments-header-renderer'),
        box_defined: !!customElements.get('bili-comment-box'),
        comment_box: !!box, editor: !!editor,
        comment_root_top: rect?.top ?? null, comment_root_height: rect?.height ?? null,
        viewport_height: innerHeight,
        comment_spinner: !!comments?.querySelector('#spinner-container'),
        comments_disabled: !!disabled?.getClientRects().length,
        editor_connected: !!editor?.isConnected,
        editor_visible: !!editor?.getClientRects().length,
        editor_focused: !!editor && editor.getRootNode().activeElement === editor};
    }""", timeout=2_000)


class _PreparationNetworkDiagnostics:
    """Observe failure metadata without retaining URLs, headers or bodies."""
    def __init__(self, page):
        self.page, self.events, self.counts = page, [], {}
        self.comment_api_statuses = {}
        self.comment_api_codes = {}
        self.listeners = [('requestfailed', self.request_failed),
                          ('response', self.response), ('requestfinished', self.request_finished), ('pageerror', self.page_error)]

    @staticmethod
    def route(request):
        parsed = urlparse(request.url)
        host = (parsed.hostname or '').lower()
        if not any(host == domain or host.endswith('.' + domain)
                   for domain in ('bilibili.com', 'hdslb.com', 'biliapi.net')):
            return None
        if parsed.path.startswith('/x/v2/reply'):
            return 'comment_api'
        if request.resource_type == 'script':
            return 'script'
        if request.resource_type == 'document':
            return 'document'
        return None

    def add(self, event, **fields):
        self.counts[event] = self.counts.get(event, 0) + 1
        if len(self.events) < 12:
            self.events.append(dict(event=event, **fields))

    def request_failed(self, request):
        route = self.route(request)
        if route:
            match = re.search(r'net::[A-Z0-9_]+', request.failure or '')
            self.add('request_failed', route=route, error=match.group() if match else 'transport_error')

    def response(self, response):
        route = self.route(response.request)
        if route == 'comment_api':
            key = str(response.status)
            self.comment_api_statuses[key] = self.comment_api_statuses.get(key, 0) + 1
        if route and response.status >= 400:
            self.add('http_error', route=route, status=response.status)

    def request_finished(self, request):
        # Read only completed reply-list requests; HTTP 200 can still contain a
        # business error. Never retain reply text, URLs, headers or user details.
        if self.route(request) != 'comment_api' or request.method != 'GET':
            return
        try:
            response = request.response()
            payload = response.json() if response is not None else None
            code = payload.get('code') if isinstance(payload, dict) else None
            key = str(code) if type(code) is int else 'missing_code'
            self.comment_api_codes[key] = self.comment_api_codes.get(key, 0) + 1
        except Exception:
            self.add('comment_api_body_unavailable')

    def page_error(self, _error):
        self.add('page_error')  # Exception messages can contain private URLs.

    def __enter__(self):
        for event, callback in self.listeners:
            self.page.on(event, callback)
        return self

    def __exit__(self, *_):
        for event, callback in self.listeners:
            self.page.remove_listener(event, callback)


def _remaining_timeout(deadline, limit_ms):
    remaining = int((deadline - time.monotonic()) * 1000)
    if remaining <= 0:
        raise PlaywrightTimeoutError('Comment preparation budget exhausted')
    return min(limit_ms, remaining)


def _comment_failure_classification(state, diagnosis):
    """Describe observed structure, without claiming an unobserved root cause."""
    state = state if isinstance(state, dict) else {}
    diagnosis = diagnosis if isinstance(diagnosis, dict) else {}
    if diagnosis.get('classification') == 'playwright_dom_access_failed_native_responsive':
        return 'playwright_dom_access_failed_native_responsive'
    if state.get('comments_disabled') is True:
        return 'comments_disabled'
    if state.get('comment_root') is True:
        if state.get('component_defined') is False:
            return 'comment_component_not_registered'
        if state.get('comment_header') is False:
            return 'comment_header_not_rendered'
        if state.get('comment_box') is False:
            return 'main_comment_box_not_rendered'
        if state.get('editor_visible') is False:
            return 'main_comment_editor_not_visible'
    return 'undetermined'


def _ready_comment_editor(page, deadline, attempt=1):
    """Recheck and focus the current main editor without navigation or typing."""
    stage = 'comment_permission'
    try:
        with audit_step('comment_prepare_comment_permission', 'browser-flow.jsonl', attempt=attempt):
            state = page.wait_for_function(_COMMENT_INPUT_STATE,
                polling=250, timeout=_remaining_timeout(deadline, 15_000)).json_value()
            if state in ('disabled', 'disabled_by_author'):
                raise CommentUnavailable('comments_disabled_by_author' if state == 'disabled_by_author'
                                         else 'comments_disabled')
        stage = 'editor_resolve'
        with audit_step('comment_prepare_editor_resolve', 'browser-flow.jsonl', attempt=attempt):
            editor = page.locator('bili-comments').evaluate_handle("""root => {
              const box = (""" + _MAIN_COMMENT_BOX + """)(root);
              return box?.shadowRoot?.querySelector('bili-comment-rich-textarea')
                ?.shadowRoot?.querySelector('.brt-editor[contenteditable=true]') || null;
            }""", timeout=_remaining_timeout(deadline, 5_000)).as_element()
            if not editor:
                raise RuntimeError('comment editor unavailable')
        stage = 'editor_click'
        with audit_step('comment_prepare_editor_click', 'browser-flow.jsonl', attempt=attempt):
            editor.wait_for_element_state('visible', timeout=_remaining_timeout(deadline, 10_000))
            editor.click(timeout=_remaining_timeout(deadline, 10_000))
        stage = 'editor_focus'
        with audit_step('comment_prepare_editor_focus', 'browser-flow.jsonl', attempt=attempt):
            focused = page.locator('bili-comments').evaluate("""root => {
              const box = (""" + _MAIN_COMMENT_BOX + """)(root);
              const e = box?.shadowRoot?.querySelector('bili-comment-rich-textarea')
                ?.shadowRoot?.querySelector('.brt-editor[contenteditable=true]');
              return !!e && e.isConnected && e.getRootNode().activeElement === e;
            }""", timeout=_remaining_timeout(deadline, 2_000))
            if not focused:
                raise RuntimeError('comment editor did not receive focus')
        return editor
    except Exception as exc:
        raise CommentPreparationError(stage, attempt, type(exc).__name__,
                                      exc.reason if isinstance(exc, CommentUnavailable) else '') from exc


def _prepare_comment_editor(page: Page, url: str, *, budget_seconds: float = 90, failure_probe=None, replace_page=None):
    """At most three navigations before typing, sharing a preparation budget."""
    if not 0 < budget_seconds <= 90:
        raise ValueError('Invalid comment preparation budget')
    started = time.monotonic()
    deadline = started + budget_seconds
    preparation_id = uuid4().hex
    failures = []
    with _PreparationNetworkDiagnostics(page) as network:
        try:
            for attempt in (1, 2, 3):
                api_statuses_before = network.comment_api_statuses.copy()
                api_codes_before = network.comment_api_codes.copy()
                # Give each attempt its own bounded share. A stalled CDP connection
                # may still overshoot an individual Playwright timeout.
                attempt_deadline = min(deadline, time.monotonic() + budget_seconds / 3)
                stage = 'navigation'
                try:
                    with audit_step('comment_prepare_navigation', 'browser-flow.jsonl',
                                    preparation_id=preparation_id, attempt=attempt) as navigation:
                        # Reload the same verified target; a newly created task
                        # page still needs navigation. Never reload after typing.
                        reload_target = (attempt > 1 and isinstance(page.url, str)
                                         and _same_comment_target(url, page.url))
                        navigation['action'] = 'reload' if reload_target else 'goto'
                        response = (page.reload(wait_until='domcontentloaded',
                                    timeout=_remaining_timeout(attempt_deadline, 30_000)) if reload_target
                                    else page.goto(url, wait_until='domcontentloaded',
                                    timeout=_remaining_timeout(attempt_deadline, 30_000)))
                        status = response.status if response is not None else None
                        navigation['document_http_status'] = status if type(status) is int else None
                        current_url = page.url
                        navigation['same_video_target'] = (_same_comment_target(url, current_url)
                                                           if isinstance(current_url, str) else None)
                        navigation['page_closed'] = page.is_closed() is True
                        if type(status) is int and status >= 400:
                            raise CommentPreparationError('navigation', attempt, 'HTTPError', 'document_http_error')
                        if isinstance(current_url, str) and not _same_comment_target(url, current_url):
                            raise CommentPreparationError('navigation', attempt, 'UnexpectedNavigation', 'unexpected_page_target')
                    stage = 'dom_probe'
                    # A document element must already exist after DOMContentLoaded.
                    # Detect a stalled utility context before spending 15s waiting
                    # for a component that the native renderer may already have.
                    with audit_step('comment_prepare_dom_probe', 'browser-flow.jsonl',
                                    preparation_id=preparation_id, attempt=attempt):
                        page.locator('html').evaluate('e => !!e',
                            timeout=_remaining_timeout(attempt_deadline, 5_000))
                    stage = 'comment_section'
                    with audit_step('comment_prepare_comment_section', 'browser-flow.jsonl', attempt=attempt):
                        _scroll_to_comment_section(page, reload_once=False, deadline=attempt_deadline)
                    editor = _ready_comment_editor(page, attempt_deadline, attempt)
                    write_audit_event(ROOT/'logs/browser-flow.jsonl', 'comment_preparation_ready',
                                      preparation_id=preparation_id, attempt=attempt,
                                      recovered=bool(failures), previous_failures=len(failures),
                                      elapsed_ms=round((time.monotonic()-started)*1000))
                    return editor
                except Exception as exc:
                    stage = exc.stage if isinstance(exc, CommentPreparationError) else stage
                    error_type = exc.error_type if isinstance(exc, CommentPreparationError) else type(exc).__name__
                    failures.append(dict(stage=stage, attempt=attempt, error_type=error_type))
                    fields = dict(stage=stage, attempt=attempt, error_type=error_type,
                                  preparation_id=preparation_id,
                                  elapsed_ms=round((time.monotonic()-started)*1000))
                    write_audit_event(ROOT/'logs/browser-flow.jsonl', 'comment_preparation_error',
                                      error_detail=str(exc)[:2000], **fields)
                    diagnosis, state = {}, {}
                    if failure_probe is not None and error_type == 'TimeoutError' and stage in ('navigation', 'dom_probe', 'comment_section', 'comment_permission', 'editor_resolve', 'editor_click') and deadline - time.monotonic() > 8:
                        try:
                            diagnosis = failure_probe(page, preparation_id=preparation_id, attempt=attempt) or {}
                            if not isinstance(diagnosis, dict):
                                diagnosis = {}
                        except Exception as diagnostic_error:
                            write_audit_event(ROOT/'logs/browser-flow.jsonl', 'comment_diagnostics_unavailable',
                                              preparation_id=preparation_id, attempt=attempt,
                                              error_type=type(diagnostic_error).__name__)
                    # A known broken DOM channel cannot provide supplementary
                    # structure; preserve the remaining budget for recovery.
                    if diagnosis.get('classification') != 'playwright_dom_access_failed_native_responsive' and time.monotonic() < deadline:
                        try:
                            state = _comment_page_state(page)
                            write_audit_event(ROOT/'logs/browser-flow.jsonl', 'comment_preparation_diagnostics',
                                              state=state, **fields)
                        except Exception as diagnostic_error:
                            write_audit_event(ROOT/'logs/browser-flow.jsonl', 'comment_diagnostics_unavailable',
                                              preparation_id=preparation_id, error_type=type(diagnostic_error).__name__)
                    classification = _comment_failure_classification(state, diagnosis)
                    failures[-1]['classification'] = classification
                    write_audit_event(ROOT/'logs/browser-flow.jsonl', 'comment_failure_analysis',
                                      classification=classification,
                                      comment_api_statuses={k: v-api_statuses_before.get(k, 0) for k, v in network.comment_api_statuses.items() if v > api_statuses_before.get(k, 0)},
                                      comment_api_codes={k: v-api_codes_before.get(k, 0) for k, v in network.comment_api_codes.items() if v > api_codes_before.get(k, 0)},
                                      **fields)
                    if error_type == 'TimeoutError' and classification == 'comments_disabled':
                        # The disabled box can arrive just after readiness times
                        # out. Its visible state is an explicit permission result,
                        # so skip the candidate instead of reloading a denied page.
                        error = CommentPreparationError('comment_permission', attempt,
                                                        'CommentUnavailable', 'comments_disabled')
                        error.failures = failures
                        error.failure_classification = classification
                        raise error from exc
                    if error_type == 'TimeoutError' and attempt < 3 and time.monotonic() < deadline:
                        recovery = 'navigate_again_before_typing'
                        if replace_page is not None and (attempt == 2 or classification in (
                                'playwright_dom_access_failed_native_responsive', 'comment_header_not_rendered',
                                'main_comment_box_not_rendered', 'comment_component_not_registered')):
                            network.__exit__()
                            page = replace_page(page)
                            network.page = page
                            network.__enter__()
                            recovery = 'replace_owned_page_before_typing'
                        write_audit_event(ROOT/'logs/browser-flow.jsonl', 'comment_preparation_retry',
                                          recovery=recovery, classification=classification, **fields)
                        continue
                    error = exc if isinstance(exc, CommentPreparationError) else CommentPreparationError(stage, attempt, error_type)
                    error.failures = failures
                    error.failure_classification = classification
                    raise error from exc
        finally:
            write_audit_event(ROOT/'logs/browser-flow.jsonl', 'comment_preparation_network',
                              preparation_id=preparation_id, budget_seconds=budget_seconds,
                              elapsed_ms=round((time.monotonic()-started)*1000),
                              failure_counts=network.counts, failures=network.events,
                              comment_api_statuses=network.comment_api_statuses,
                              comment_api_codes=network.comment_api_codes)


def _recheck_prepared_comment_editor(page):
    """Revalidate a retained page once; never reload after the durable intent."""
    with audit_step('comment_prepared_page_recheck', 'browser-flow.jsonl'):
        try:
            return _ready_comment_editor(page, time.monotonic() + 15)
        except CommentPreparationError as exc:
            write_audit_event(ROOT/'logs/browser-flow.jsonl', 'comment_preparation_error',
                              stage=exc.stage, attempt=exc.attempts, error_type=exc.error_type,
                              reused_page=True)
            try:
                write_audit_event(ROOT/'logs/browser-flow.jsonl', 'comment_preparation_diagnostics',
                                  stage=exc.stage, reused_page=True, state=_comment_page_state(page))
            except Exception as diagnostic_error:
                write_audit_event(ROOT/'logs/browser-flow.jsonl', 'comment_diagnostics_unavailable',
                                  error_type=type(diagnostic_error).__name__, reused_page=True)
            raise


@dataclass(frozen=True)
class Candidate:
    bvid: str
    url: str
    title: str
    author: str


class BilibiliClient:
    """Connection-only client. Chrome lifecycle belongs to the external CDP manager."""
    def __init__(self, cdp_url: str) -> None:
        self.cdp_url = cdp_url.rstrip("/")
        self._pw = None
        self._browser = None
        self.context: BrowserContext | None = None
        self.last_submitted_comment_id: str | None = None
        self._prepared_comment = None
        self._lease = None

    @audited('browser_connect', 'browser-flow.jsonl')
    def __enter__(self) -> "BilibiliClient":
        endpoint = self.cdp_url
        parsed = urlparse(endpoint)
        connection_id = uuid4().hex
        target = f"{parsed.hostname}:{parsed.port}" if parsed.hostname else parsed.scheme
        log_path = ROOT / "logs" / "cdp-connections.jsonl"
        started = phase_started = time.perf_counter()
        phase = "discovery"

        def log(event: str, **fields: object) -> None:
            write_audit_event(log_path, event, connection_id=connection_id, target=target, **fields)

        try:
            phase = 'browser_lease'
            self._lease = BrowserLease(self.cdp_url)
            self._lease.acquire()
            phase = 'discovery'
            if parsed.scheme in ('http', 'https') and parsed.hostname in ('127.0.0.1', 'localhost', '::1'):
                # Resolve local DevTools directly, never through HTTP_PROXY.
                log("cdp_phase_started", phase=phase, timeout_ms=5_000)
                try:
                    with build_opener(ProxyHandler({})).open(endpoint + '/json/version', timeout=5) as response:
                        endpoint = json.load(response)['webSocketDebuggerUrl']
                    ws = urlparse(endpoint)
                    if ws.scheme not in ('ws', 'wss') or ws.hostname not in ('127.0.0.1', 'localhost', '::1'):
                        raise ValueError('DevTools did not return a loopback WebSocket endpoint')
                except Exception as exc:
                    raise RuntimeError(f'本地 CDP 服务不可用：{self.cdp_url}；请检查浏览器配置是否已启动（{type(exc).__name__}）') from exc
                log("cdp_phase_completed", phase=phase, elapsed_ms=round((time.perf_counter() - phase_started) * 1000))
                phase = "profile_info_preflight"
                phase_started = time.perf_counter()
                log("cdp_phase_started", phase=phase)
                if _recover_stalled_profile_info_tab(self.cdp_url, endpoint):
                    log("cdp_profile_info_recovered")
                log("cdp_phase_completed", phase=phase, elapsed_ms=round((time.perf_counter() - phase_started) * 1000))
            phase = "playwright_start"
            phase_started = time.perf_counter()
            log("cdp_phase_started", phase=phase)
            self._pw = sync_playwright().start()
            log("cdp_phase_completed", phase=phase, elapsed_ms=round((time.perf_counter() - phase_started) * 1000))
            phase = "connect_over_cdp"
            phase_started = time.perf_counter()
            log("cdp_phase_started", phase=phase, timeout_ms=60_000)
            self._browser = self._pw.chromium.connect_over_cdp(endpoint, timeout=60_000)
            log("cdp_phase_completed", phase=phase, elapsed_ms=round((time.perf_counter() - phase_started) * 1000))
            phase = "context_selection"
            phase_started = time.perf_counter()
            log("cdp_phase_started", phase=phase)
            if not self._browser.contexts:
                raise RuntimeError("CDP browser has no available context")
            self.context = self._browser.contexts[0]
            log("cdp_phase_completed", phase=phase, elapsed_ms=round((time.perf_counter() - phase_started) * 1000))
            log("cdp_connection_ready", elapsed_ms=round((time.perf_counter() - started) * 1000))
            return self
        except BaseException as exc:
            log("cdp_connection_failed", phase=phase,
                phase_elapsed_ms=round((time.perf_counter() - phase_started) * 1000),
                total_elapsed_ms=round((time.perf_counter() - started) * 1000),
                error_type=type(exc).__name__)
            # __exit__ is not called by Python when __enter__ raises.
            try:
                self.__exit__()
            except Exception:
                pass  # Preserve the original connection error.
            raise

    @audited('browser_detach', 'browser-flow.jsonl')
    def __exit__(self, *_: object) -> None:
        # A CDP connection must only detach; never close the user-managed browser.
        try:
            self.discard_prepared_comment()
            pw, self._pw = self._pw, None
            self.context = None
            self._browser = None
            if pw:
                pw.stop()
        finally:
            if self._lease is not None:
                self._lease.release()
                self._lease = None

    def page(self) -> Page:
        if not self.context:
            raise RuntimeError("CDP client is not connected")
        return self.context.new_page()

    @audited('video_visibility', 'browser-flow.jsonl')
    def video_visible(self, bvid: str) -> bool:
        if not self.context:
            raise RuntimeError("CDP client is not connected")
        # Retry only a timed-out read. An HTTP/business rejection is not a
        # transient timeout and must never be treated as a deleted video.
        deadline = time.monotonic() + 30
        for attempt in (1, 2, 3):
            try:
                with audit_step('video_visibility_attempt', 'browser-flow.jsonl',
                                bvid=bvid, attempt=attempt):
                    response = self.context.request.get(
                        "https://api.bilibili.com/x/web-interface/view",
                        params={"bvid": bvid}, timeout=_remaining_timeout(deadline, 10_000),
                    )
                break
            except PlaywrightTimeoutError as exc:
                if attempt == 3 or time.monotonic() >= deadline:
                    raise CommentPreparationError('video_visibility', attempt, 'TimeoutError') from exc
                write_audit_event(ROOT/'logs/browser-flow.jsonl', 'video_visibility_retry',
                                  bvid=bvid, attempt=attempt, recovery='retry_read_only_request')
        if not response.ok:
            raise RuntimeError(f"Bilibili video check HTTP {response.status}: {bvid}")
        payload = response.json()
        if payload.get("code") == 0:
            return True
        if payload.get("code") == 62002:
            return False
        raise RuntimeError(f"Bilibili video check rejected: code={payload.get('code')} bvid={bvid}")

    @audited('browser_login', 'browser-flow.jsonl')
    def uid(self) -> str | None:
        if not self.context:
            raise RuntimeError("CDP client is not connected")
        # The context request shares this browser's cookie jar. Identity does
        # not require loading a media-heavy homepage or a working DOM channel.
        deadline = time.monotonic() + 30
        source = 'authenticated_context_request'
        for attempt in (1, 2, 3):
            try:
                with audit_step('browser_identity_request', 'browser-flow.jsonl', attempt=attempt):
                    response = self.context.request.get(
                        'https://api.bilibili.com/x/web-interface/nav',
                        timeout=_remaining_timeout(deadline, 10_000))
                break
            except PlaywrightTimeoutError:
                if attempt == 3 or time.monotonic() >= deadline:
                    # CDP-attached browsers can have a different network/proxy
                    # path from APIRequestContext. Fall back to the browser's
                    # own authenticated fetch, once, without waiting for the
                    # entire homepage to render. Never use this on rejection.
                    payload = self._identity_from_page()
                    source = 'authenticated_page_fetch'
                    break
                write_audit_event(ROOT/'logs/browser-flow.jsonl', 'browser_identity_retry',
                                  attempt=attempt, recovery='retry_authenticated_read')
        if source == 'authenticated_context_request':
            if not response.ok:
                raise RuntimeError(f'Bilibili identity check HTTP {response.status}')
            payload = response.json()
        code = payload.get('code')
        data = payload.get('data')
        if code == -101 or (code == 0 and isinstance(data, dict) and data.get('isLogin') is False):
            write_audit_event(ROOT/'logs/browser-flow.jsonl', 'login_result', logged_in=False)
            return None
        if code != 0 or not isinstance(data, dict) or data.get('isLogin') is not True:
            raise RuntimeError('Bilibili identity response rejected or invalid')
        mid = data.get('mid')
        if type(mid) not in (int, str) or not re.fullmatch(r'[1-9][0-9]*', str(mid)):
            raise RuntimeError('Bilibili identity UID invalid')
        write_audit_event(ROOT/'logs/browser-flow.jsonl', 'login_result', logged_in=True,
                          source=source)
        return str(mid)

    def _identity_from_page(self):
        page = self.page()
        try:
            with audit_step('browser_identity_page_fallback', 'browser-flow.jsonl'):
                page.goto('https://www.bilibili.com/', wait_until='commit', timeout=10_000)
                parsed = urlparse(page.url)
                if parsed.scheme != 'https' or parsed.hostname != 'www.bilibili.com':
                    raise RuntimeError('Unexpected identity page target')
                result = page.locator('html').evaluate("""async () => {
                  const r = await fetch('https://api.bilibili.com/x/web-interface/nav',
                    {credentials:'include', signal:AbortSignal.timeout(10000)});
                  return {ok:r.ok, payload:await r.json()};
                }""", timeout=15_000)
                if not isinstance(result, dict) or result.get('ok') is not True:
                    raise RuntimeError('Bilibili page identity response rejected')
                return result['payload']
        finally:
            try:
                page.close()
            except Exception as exc:
                write_audit_event(ROOT/'logs/browser-flow.jsonl', 'identity_page_cleanup_failed',
                                  error_type=type(exc).__name__)

    @audited('video_search', 'browser-flow.jsonl')
    def search(self, keyword: str, limit: int = 10, *, page_number: int = 1) -> list[Candidate]:
        if type(page_number) is not int or not 1 <= page_number <= 20 or not 1 <= limit <= 20:
            raise ValueError('Search page or limit out of range')
        page = self.page()
        try:
            page.goto(f"https://search.bilibili.com/all?keyword={quote(keyword)}&order=pubdate&page={page_number}", wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(2_000)
            rows = page.locator(".bili-video-card").evaluate_all("""cards => cards.slice(0, 20).map(c => {
              const a=c.querySelector('.bili-video-card__info--right a') || c.querySelector('a[href*=video]');
              const author=c.querySelector('.bili-video-card__info--author') || c.querySelector('.bili-video-card__info--bottom a');
              return {url:a?.href || '', title:(a?.title || a?.innerText || '').trim(), author:(author?.innerText || '').trim()};
            })""")
            result: list[Candidate] = []
            for row in rows:
                match = _BVID.search(row["url"])
                if match:
                    result.append(Candidate(match.group(1), row["url"], row["title"][:500], row["author"][:200]))
            write_audit_event(ROOT/'logs/browser-flow.jsonl', 'search_result', count=len(result[:limit]), page_number=page_number)
            return result[:limit]
        finally:
            page.close()

    @audited('video_watch', 'browser-flow.jsonl')
    def watch_and_like(self, url: str, long_watch_minimum: float = 600, long_watch_maximum: float = 900, allow_like: bool = False) -> tuple[float, float, bool]:
        page = self.page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            video = page.locator("video").first
            video.wait_for(state="attached", timeout=20_000)
            previous, actual, stalled, target = None, 0.0, 0, 0.0
            while True:
                state = video.evaluate("v => ({current:v.currentTime,duration:v.duration,paused:v.paused,ended:v.ended})")
                duration = float(state["duration"] or 0)
                if duration <= 0 or duration == float("inf"):
                    raise RuntimeError("video duration unavailable")
                if not target:
                    # Short videos are watched through to the end. Longer ones
                    # get a human-scale 10–15 minute viewing window.
                    target = duration if duration <= long_watch_minimum else min(duration, random.uniform(long_watch_minimum, long_watch_maximum))
                current = float(state["current"] or 0)
                if previous is not None and current > previous:
                    actual += current - previous; stalled = 0
                elif not state["ended"]:
                    stalled += 1
                if actual >= target or state["ended"]:
                    break
                if stalled >= 10:
                    raise RuntimeError("video progress stalled")
                previous = current
                time.sleep(3)
            liked = False
            if allow_like:
                for selector in (".video-like-info", ".toolbar-left .video-like"):
                    try:
                        button = page.locator(selector).first
                        if button.is_visible(timeout=1_000):
                            button.click(timeout=3_000); liked = True; break
                    except Exception:
                        pass
            write_audit_event(ROOT/'logs/browser-flow.jsonl', 'watch_result', watched_seconds=actual, like_attempted=allow_like, liked=liked)
            return duration, actual, liked
        finally:
            page.close()

    @audited('comment_submission', 'browser-flow.jsonl')
    def publish_comment(self, url: str, text: str) -> str:
        """Submit a comment and return evidence from the actual submit action.

        The replies listing API may hide newly submitted comments while moderation
        or indexing is pending, so it is not the sole source of truth.
        """
        # A rejected, interrupted or unconfirmed new attempt must never inherit
        # the identifier from this client's previous successful submission.
        self.last_submitted_comment_id = None
        page = self.take_prepared_comment(url)
        reused = page is not None
        page = page if reused else self.page()
        attempted = False
        try:
            if reused:
                _recheck_prepared_comment_editor(page)
                write_audit_event(ROOT/'logs/browser-flow.jsonl', 'comment_prepared_page_reused')
            else:
                _prepare_comment_editor(page, url)
            # ElementHandle has no press_sequentially API; typing through the
            # focused page keyboard produces the required input events.
            with audit_step('comment_input', 'browser-flow.jsonl'):
                page.keyboard.type(text, delay=random.randint(45, 115))
            with audit_step('comment_button_ready', 'browser-flow.jsonl'):
                button = page.locator("bili-comments").evaluate_handle("""root => {
                  const b = (""" + _MAIN_COMMENT_BOX + """)(root);
                  return b?.shadowRoot?.querySelector('#pub button, button[type=submit], .submit-btn') || null;
                }""").as_element()
                if not button:
                    raise RuntimeError("comment publish button unavailable")
                # Trial checks actionability without dispatching a click.
                button.click(trial=True, timeout=10_000)
            try:
                # Bilibili's comment request is the strongest immediate evidence
                # available to an authenticated browser session.
                with page.expect_response(lambda response: "/x/v2/reply/add" in response.url, timeout=10_000) as response_info:
                    attempted = True
                    write_audit_event(ROOT/'logs/browser-flow.jsonl', 'submit_click_started')
                    button.click(timeout=5_000)
                payload = response_info.value.json()
                code = payload.get('code')
                if type(code) not in (int, str) or (isinstance(code, str) and not code.lstrip('-').isdigit()):
                    raise ValueError('Invalid comment receipt code')
                if code in (0, '0'):
                    def comment_id(value):
                        if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]*', value):
                            return value
                        if type(value) is int and 0 < value <= 9_007_199_254_740_991:
                            return str(value)
                        return None

                    data = payload.get('data')
                    if isinstance(data, dict):
                        reply = data.get('reply')
                        if isinstance(reply, dict):
                            self.last_submitted_comment_id = (comment_id(reply.get('rpid_str'))
                                                              or comment_id(reply.get('rpid')))
                        if self.last_submitted_comment_id is None:
                            self.last_submitted_comment_id = comment_id(data.get('rpid'))
                    # A missing identifier does not revoke the accepted receipt.
                    write_audit_event(ROOT/'logs/browser-flow.jsonl', 'submission_accepted',
                                      comment_id_available=self.last_submitted_comment_id is not None)
                    return "network_confirmed"
                raise SubmissionRejected(f"comment API rejected submission: code={payload.get('code')}")
            except PlaywrightTimeoutError:
                # The endpoint can change; retain a conservative pending state
                # rather than treating an already-clicked submission as failed.
                page.wait_for_timeout(1_500)
                feedback = page.locator("body").inner_text(timeout=3_000)
                if any(word in feedback for word in ("评论发布成功", "发布成功", "发送成功")):
                    return "ui_confirmed"
                return "submitted_unconfirmed"
        except SubmissionRejected:
            raise
        except Exception as exc:
            if attempted:
                raise SubmissionUncertain(f'Comment submission outcome unknown ({type(exc).__name__})') from None
            raise
        finally:
            try:
                page.close()
            except Exception as exc:
                write_audit_event(ROOT/'logs/browser-flow.jsonl', 'submission_page_cleanup_failed', error_type=type(exc).__name__)

    def discard_prepared_comment(self) -> None:
        prepared, self._prepared_comment = self._prepared_comment, None
        if prepared:
            try:
                prepared['page'].close()
            except Exception as exc:
                write_audit_event(ROOT/'logs/browser-flow.jsonl', 'submission_page_cleanup_failed',
                                  error_type=type(exc).__name__)

    def take_prepared_comment(self, url: str):
        """Consume an owned page only when URL, context and short freshness agree."""
        prepared = self._prepared_comment
        if not prepared:
            return None
        page = prepared['page']
        checks = dict(request_target=_same_comment_target(prepared['url'], url),
                      same_context=self.context is not None and page.context is self.context,
                      page_open=not page.is_closed(), page_target=_same_comment_target(url, page.url),
                      fresh=time.monotonic() - prepared['created'] <= 30)
        if not all(checks.values()):
            write_audit_event(ROOT/'logs/browser-flow.jsonl', 'comment_prepared_page_discarded', checks=checks)
            self.discard_prepared_comment()
            return None
        self._prepared_comment = None
        return page

    def diagnose_comment_runtime(self, page, **fields) -> None:
        """Independent bounded CDP read; no DOM text, cookies or user-tab changes."""
        result = {}
        try:
            parsed = urlparse(self.cdp_url)
            if parsed.scheme not in ('http', 'https') or parsed.hostname not in ('localhost','127.0.0.1','::1'):
                return
            with build_opener(ProxyHandler({})).open(self.cdp_url.rstrip('/') + '/json/list', timeout=3) as response:
                targets = json.load(response)
            matches = [t for t in targets if t.get('type') == 'page' and t.get('url') == page.url]
            if len(matches) != 1:
                result['status'] = 'ambiguous_target'
            else:
                endpoint = matches[0]['webSocketDebuggerUrl']
                native = _cdp_command(endpoint, 'Runtime.evaluate', {
                    'expression': '({html:!!document.documentElement, ready:document.readyState, hidden:document.hidden, comment:!!document.querySelector("bili-comments"), player:!!document.querySelector(".bpx-player-container")})',
                    'returnByValue': True, 'timeout': 2000}, timeout=3)
                value = native.get('result', {}).get('value')
                result['native_state'] = value if isinstance(value, dict) else None
                result['status'] = 'native_main_responsive' if isinstance(value, dict) else 'native_evaluation_failed'
                try:
                    result['playwright_html'] = page.locator('html').evaluate('e => !!e', timeout=1000) is True
                except Exception as exc:
                    result['playwright_error'] = type(exc).__name__
                if isinstance(value, dict) and value.get('html') and result.get('playwright_error'):
                    result['classification'] = 'playwright_dom_access_failed_native_responsive'
        except Exception as exc:
            result.update(status='native_probe_failed', error_type=type(exc).__name__)
        write_audit_event(ROOT/'logs/browser-flow.jsonl', 'comment_runtime_diagnosis', **fields, **result)
        return result

    @audited('comment_editor_check', 'browser-flow.jsonl')
    def check_comment_editor(self, url: str, *, retain: bool = False, budget_seconds: float = 90) -> dict:
        """Prepare without typing; optionally retain this client-owned page."""
        self.discard_prepared_comment()
        page = self.page()
        retained = False
        def replace_owned_page(old):
            nonlocal page
            replacement = self.page()
            page = replacement
            try:
                old.close()
            except Exception as exc:
                write_audit_event(ROOT/'logs/browser-flow.jsonl', 'stalled_owned_page_cleanup_failed', error_type=type(exc).__name__)
            return replacement
        try:
            _prepare_comment_editor(page, url, budget_seconds=budget_seconds,
                                    failure_probe=self.diagnose_comment_runtime, replace_page=replace_owned_page)
            try:
                state = _comment_page_state(page)
            except Exception as exc:
                # A diagnostic read is supplementary; readiness and focus were
                # already checked. Do not discard that success for a log read.
                write_audit_event(ROOT/'logs/browser-flow.jsonl', 'comment_diagnostics_unavailable',
                                  error_type=type(exc).__name__)
                state = dict(editor=True, diagnostics_available=False)
            if retain:
                if self.context is None or page.context is not self.context:
                    raise RuntimeError('Cannot retain a page outside the connected context')
                self._prepared_comment = dict(page=page, url=url, created=time.monotonic())
                retained = True
                write_audit_event(ROOT/'logs/browser-flow.jsonl', 'comment_prepared_page_retained', ttl_seconds=30)
            return state
        finally:
            if not retained:
                try:
                    page.close()
                except Exception as exc:
                    write_audit_event(ROOT/'logs/browser-flow.jsonl', 'submission_page_cleanup_failed',
                                      error_type=type(exc).__name__)

    @audited('comment_verification', 'browser-flow.jsonl')
    def verify_comment(self, bvid: str, uid: str, text: str, resource_url: str, remote_comment_id: str | None = None) -> str | None:
        """Compatibility wrapper: uncertainty never revokes an accepted submit."""
        result = self.verify_comment_details(bvid, uid, text, resource_url, remote_comment_id)
        if result['status'] == 'error':
            raise RuntimeError(f"Comment verification error: {result['reason']} ({result.get('error_type', 'Unknown')})")
        return result['comment_id'] if result['status'] == 'verified' else None

    @audited('comment_verification_details', 'browser-flow.jsonl')
    def verify_comment_details(self, bvid: str, uid: str, text: str, resource_url: str,
                               remote_comment_id: str | None = None) -> dict:
        """Read exact comment evidence from official APIs without any submit action.

        An API-readable comment is not a claim that a DOM renderer or every viewer
        can see it. A limited search miss is unconfirmed, never proof of deletion.
        ``resource_url`` remains in the signature for existing callers; the full
        stored text hash already includes it and is never written to the audit log.
        """
        result = dict(status='unconfirmed', method='metadata', comment_id=None,
                      reason='not_found_in_checked_scope', api_code=None, error_type=None,
                      source='official_comment_api', api_results=[],
                      scope=dict(aid_available=False, root_requested=bool(remote_comment_id),
                                 root_comments_checked=0, listing_pages=0,
                                 listing_comments_checked=0, sorts_checked=[],
                                 max_pages_per_sort=5, page_size=20, max_elapsed_seconds=45))
        page = None
        deadline = time.monotonic() + 45
        root_unavailable = False
        root_error = None
        digest = hashlib.sha256(text.encode()).hexdigest()

        def identifier(value):
            # JSON numbers above the safe JavaScript integer range may be rounded.
            # Prefer the platform's string identifier when one is supplied.
            if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]*', value):
                return value
            if type(value) is int and 0 < value <= 9_007_199_254_740_991:
                return str(value)
            return None

        expected_uid = identifier(uid)
        expected_rpid = identifier(remote_comment_id) if remote_comment_id is not None else None

        def failure(reason, method, error_type, api_code=None):
            result.update(status='error', method=method, reason=reason,
                          error_type=error_type, api_code=api_code)
            return result

        def api(path, stage, *, sort=None, pn=None):
            remaining = int((deadline - time.monotonic()) * 1000)
            timeout_ms = max(0, min(10_000, remaining))
            fields = dict(timeout_ms=timeout_ms)
            if type(sort) is int:
                fields['sort'] = sort
            if type(pn) is int:
                fields['pn'] = pn
            receipt = dict(stage=stage, http_status=None, code=None)
            started = time.perf_counter()
            try:
                with audit_step('comment_verification_' + stage, 'browser-flow.jsonl', **fields) as audit:
                    if remaining <= 0:
                        raise TimeoutError('verification_time_budget_exhausted')
                    response = page.evaluate("""async ({url, timeout_ms}) => {
                      const response = await fetch(url, {credentials:'include',
                        signal:AbortSignal.timeout(timeout_ms)});
                      return {http_status:response.status, payload:await response.json()};
                    }""", {'url': 'https://api.bilibili.com/' + path, 'timeout_ms': timeout_ms})
                    if not isinstance(response, dict) or type(response.get('http_status')) is not int:
                        raise ValueError('invalid_comment_api_response')
                    receipt['http_status'] = response['http_status']
                    payload = response.get('payload')
                    code = payload.get('code') if isinstance(payload, dict) else None
                    if isinstance(code, str) and re.fullmatch(r'-?[0-9]+', code):
                        code = int(code)
                    receipt['code'] = code if type(code) is int else None
                    audit.update(http_status=receipt['http_status'], api_code=receipt['code'])
                    if response['http_status'] != 200:
                        raise RuntimeError('comment_api_http_error')
                    if not isinstance(payload, dict) or type(code) is not int:
                        raise ValueError('invalid_comment_api_response')
                    return code, payload.get('data')
            except Exception as exc:
                receipt['error_type'] = type(exc).__name__
                raise
            finally:
                receipt['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 2)
                result['api_results'].append(receipt)

        def matches(reply):
            if not isinstance(reply, dict):
                return None
            rpid = identifier(reply.get('rpid_str')) or identifier(reply.get('rpid'))
            member = reply.get('member')
            member_uid = identifier(member.get('mid')) if isinstance(member, dict) else None
            direct_uid = identifier(reply.get('mid'))
            author = member_uid or direct_uid
            if (not rpid or author != expected_uid
                    or (member_uid and direct_uid and member_uid != direct_uid)
                    or (expected_rpid and rpid != expected_rpid)):
                return None
            content = reply.get('content')
            message = content.get('message') if isinstance(content, dict) else None
            if not isinstance(message, str) or hashlib.sha256(message.encode()).hexdigest() != digest:
                return None
            return rpid

        def verified(comment_id, method):
            result.update(status='verified', method=method, comment_id=comment_id,
                          reason='identity_and_full_text_match', api_code=0, error_type=None)
            return result

        try:
            if not expected_uid or (remote_comment_id is not None and not expected_rpid):
                return failure('invalid_verification_identifier', 'metadata', 'ValueError')
            try:
                page = self.page()
            except Exception as exc:
                return failure('verification_page_creation_error', 'navigation', type(exc).__name__)
            try:
                page.goto(f'https://www.bilibili.com/video/{bvid}/', wait_until='domcontentloaded',
                          timeout=min(30_000, max(1, int((deadline - time.monotonic()) * 1000))))
            except Exception as exc:
                return failure('verification_navigation_error', 'navigation', type(exc).__name__)
            try:
                code, data = api('x/web-interface/view?bvid=' + quote(bvid, safe=''), 'metadata')
            except Exception as exc:
                return failure('video_metadata_api_error', 'metadata', type(exc).__name__)
            if code != 0:
                return failure('video_metadata_api_error', 'metadata', 'CommentApiError', code)
            aid = identifier(data.get('aid')) if isinstance(data, dict) else None
            if not aid:
                return failure('video_metadata_unavailable', 'metadata', 'ValueError', code)
            result['scope']['aid_available'] = True

            if expected_rpid:
                try:
                    code, data = api(f'x/v2/reply/detail?type=1&oid={aid}&root={expected_rpid}', 'root_detail')
                    if code == 0:
                        root = data.get('root') if isinstance(data, dict) else None
                        if not isinstance(root, dict):
                            root_error = dict(reason='root_detail_metadata_unavailable', error_type='ValueError', api_code=code)
                        else:
                            result['scope']['root_comments_checked'] += 1
                            remote = matches(root)
                            if remote:
                                return verified(remote, 'root_detail')
                    elif code == 12006:
                        root_unavailable = True
                    else:
                        root_error = dict(reason='root_detail_api_error', error_type='CommentApiError', api_code=code)
                except Exception as exc:
                    root_error = dict(reason='root_detail_api_error', error_type=type(exc).__name__, api_code=None)

            for sort in (0, 2):
                result['scope']['sorts_checked'].append(sort)
                for pn in range(1, 6):
                    try:
                        code, data = api(f'x/v2/reply?type=1&oid={aid}&sort={sort}&pn={pn}&ps=20',
                                         'listing', sort=sort, pn=pn)
                    except Exception as exc:
                        reason = ('verification_time_budget_exhausted' if time.monotonic() >= deadline
                                  else 'comment_listing_api_error')
                        return failure(reason, 'listing', type(exc).__name__)
                    result['scope']['listing_pages'] += 1
                    if code != 0:
                        return failure('comment_listing_api_error', 'listing', 'CommentApiError', code)
                    if not isinstance(data, dict):
                        return failure('comment_listing_metadata_unavailable', 'listing', 'ValueError', code)
                    if 'replies' not in data:
                        return failure('comment_listing_metadata_unavailable', 'listing', 'ValueError', code)
                    replies = data.get('replies')
                    if replies is None:
                        replies = []
                    if not isinstance(replies, list):
                        return failure('comment_listing_metadata_unavailable', 'listing', 'ValueError', code)
                    for reply in replies:
                        result['scope']['listing_comments_checked'] += 1
                        remote = matches(reply)
                        if remote:
                            return verified(remote, 'listing')
                    pagination = data.get('page')
                    count = pagination.get('count') if isinstance(pagination, dict) else None
                    if not replies or (type(count) is int and count >= 0 and pn * 20 >= count):
                        break
            if root_error:
                return failure(root_error['reason'], 'root_detail', root_error['error_type'], root_error['api_code'])
            result.update(method='listing', reason='remote_comment_unavailable' if root_unavailable
                          else 'not_found_in_checked_scope', api_code=12006 if root_unavailable else 0)
            return result
        finally:
            # Cleanup must not turn positive evidence or a scoped miss into a
            # different outcome, and must never cause a repeat submission.
            if page is not None:
                try:
                    page.close()
                except Exception as exc:
                    result['cleanup_error_type'] = type(exc).__name__
                    write_audit_event(ROOT/'logs/browser-flow.jsonl', 'verification_page_cleanup_failed',
                                      error_type=type(exc).__name__)
            write_audit_event(ROOT/'logs/browser-flow.jsonl', 'verification_result',
                              found=result['status'] == 'verified', status=result['status'],
                              method=result['method'], reason=result['reason'], bvid=bvid,
                              api_code=result['api_code'], error_type=result['error_type'],
                              scope=result['scope'], api_results=result['api_results'])
