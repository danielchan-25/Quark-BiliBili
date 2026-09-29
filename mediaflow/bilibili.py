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

from mediaflow.audit import write_audit_event
from mediaflow.config import ROOT


_BVID = re.compile(r"/video/(BV[\w]+)")


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


def _scroll_to_comment_section(page: Page) -> None:
    """Wait for Bilibili's asynchronously injected comment root before scrolling."""
    selector = "#commentapp, bili-comments"
    root = page.locator(selector).first
    for attempt in range(2):
        if attempt:
            # No editor interaction or submission has happened yet, so a single
            # page reload is safe recovery from a transient component failure.
            page.reload(wait_until="domcontentloaded", timeout=30_000)
        try:
            root.wait_for(state="attached", timeout=15_000)
        except PlaywrightTimeoutError:
            if attempt == 0:
                # The component can be deferred until its region approaches the
                # viewport. Wake lazy content, then allow the DOM another chance.
                page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                try:
                    root.wait_for(state="attached", timeout=8_000)
                except PlaywrightTimeoutError:
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
            root.scroll_into_view_if_needed(timeout=10_000)
            return
        except PlaywrightTimeoutError:
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
                error_type=type(exc).__name__, error=str(exc)[:1200])
            # __exit__ is not called by Python when __enter__ raises.
            try:
                self.__exit__()
            except Exception:
                pass  # Preserve the original connection error.
            raise

    def __exit__(self, *_: object) -> None:
        # A CDP connection must only detach; never close the user-managed browser.
        pw, self._pw = self._pw, None
        self.context = None
        self._browser = None
        if pw:
            pw.stop()

    def page(self) -> Page:
        if not self.context:
            raise RuntimeError("CDP client is not connected")
        return self.context.new_page()

    def video_visible(self, bvid: str) -> bool:
        if not self.context:
            raise RuntimeError("CDP client is not connected")
        response = self.context.request.get(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid}, timeout=10_000,
        )
        if not response.ok:
            raise RuntimeError(f"Bilibili video check HTTP {response.status}: {bvid}")
        payload = response.json()
        if payload.get("code") == 0:
            return True
        if payload.get("code") == 62002:
            return False
        raise RuntimeError(f"Bilibili video check rejected: code={payload.get('code')} bvid={bvid}")

    def uid(self) -> str | None:
        page = self.page()
        try:
            page.goto("https://www.bilibili.com/", wait_until="domcontentloaded", timeout=30_000)
            value = page.evaluate("""() => fetch('https://api.bilibili.com/x/web-interface/nav', {credentials:'include', signal:AbortSignal.timeout(10000)})
                .then(r=>r.json()).then(x=>x.data?.isLogin ? String(x.data.mid) : null)""")
            return value or None
        finally:
            page.close()

    def search(self, keyword: str, limit: int = 10) -> list[Candidate]:
        page = self.page()
        try:
            page.goto(f"https://search.bilibili.com/all?keyword={quote(keyword)}&order=pubdate", wait_until="domcontentloaded", timeout=30_000)
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
            return result[:limit]
        finally:
            page.close()

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
                elif not state["paused"] and not state["ended"]:
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
            return duration, actual, liked
        finally:
            page.close()

    def publish_comment(self, url: str, text: str) -> str:
        """Submit a comment and return evidence from the actual submit action.

        The replies listing API may hide newly submitted comments while moderation
        or indexing is pending, so it is not the sole source of truth.
        """
        page = self.page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            _scroll_to_comment_section(page)
            editor_ready = """() => {
              const comments = document.querySelector('bili-comments')?.shadowRoot;
              const header = comments?.querySelector('bili-comments-header-renderer');
              const box = header?.shadowRoot?.querySelector('#commentbox bili-comment-box')
                || comments?.querySelector('#reply-commentbox bili-comment-box');
              return !!box?.shadowRoot?.querySelector('bili-comment-rich-textarea')?.shadowRoot?.querySelector('.brt-editor[contenteditable=true]');
            }"""
            try:
                page.wait_for_function(editor_ready, timeout=15_000)
            except PlaywrightTimeoutError:
                # The comment web component can finish hydrating after the
                # initial page load. Reload once so a transient component
                # state does not turn into a publication failure.
                page.reload(wait_until="domcontentloaded", timeout=30_000)
                _scroll_to_comment_section(page)
                page.wait_for_function(editor_ready, timeout=15_000)
            # Bilibili's current comment component is nested in shadow roots.
            editor = page.locator("bili-comments").evaluate_handle("""root => {
              const comments = root.shadowRoot;
              const header = comments?.querySelector('bili-comments-header-renderer');
              // Current Bilibili puts the main box under #reply-commentbox;
              // older pages exposed it under the header renderer.
              const box = header?.shadowRoot?.querySelector('#commentbox bili-comment-box')
                || comments?.querySelector('#reply-commentbox bili-comment-box');
              const rich = box?.shadowRoot?.querySelector('bili-comment-rich-textarea');
              return rich?.shadowRoot?.querySelector('.brt-editor[contenteditable=true]') || null;
            }""")
            if not editor.as_element():
                raise RuntimeError("comment editor unavailable")
            editor.as_element().click(timeout=5_000)
            # ElementHandle has no press_sequentially API; typing through the
            # focused page keyboard produces the required input events.
            page.keyboard.type(text, delay=random.randint(45, 115))
            button = page.locator("bili-comments").evaluate_handle("""root => {
              const comments = root.shadowRoot;
              const h = comments?.querySelector('bili-comments-header-renderer');
              const b = h?.shadowRoot?.querySelector('#commentbox bili-comment-box')
                || comments?.querySelector('#reply-commentbox bili-comment-box');
              return b?.shadowRoot?.querySelector('#pub button, button[type=submit], .submit-btn') || null;
            }""").as_element()
            if not button:
                raise RuntimeError("comment publish button unavailable")
            try:
                # Bilibili's comment request is the strongest immediate evidence
                # available to an authenticated browser session.
                with page.expect_response(lambda response: "/x/v2/reply/add" in response.url, timeout=10_000) as response_info:
                    button.click(timeout=5_000)
                payload = response_info.value.json()
                if int(payload.get("code", -1)) == 0:
                    return "network_confirmed"
                raise RuntimeError(f"comment API rejected submission: code={payload.get('code')} message={payload.get('message')}")
            except PlaywrightTimeoutError:
                # The endpoint can change; retain a conservative pending state
                # rather than treating an already-clicked submission as failed.
                page.wait_for_timeout(1_500)
                feedback = page.locator("body").inner_text(timeout=3_000)
                if any(word in feedback for word in ("评论发布成功", "发布成功", "发送成功")):
                    return "ui_confirmed"
                return "submitted_unconfirmed"
        finally:
            page.close()

    def verify_comment(self, bvid: str, uid: str, text: str, resource_url: str, remote_comment_id: str | None = None) -> str | None:
        page = self.page()
        try:
            video_url = f"https://www.bilibili.com/video/{bvid}/"
            if remote_comment_id and remote_comment_id.isdigit():
                page.goto(f"{video_url}?comment_on=1&comment_root_id={remote_comment_id}", wait_until="domcontentloaded", timeout=30_000)
                try:
                    _scroll_to_comment_section(page)
                    page.wait_for_function("""({text, uid}) => {
                      const roots = [document.querySelector('bili-comments')];
                      while (roots.length) {
                        const node = roots.shift();
                        if (!node) continue;
                        if (node.nodeType === Node.ELEMENT_NODE && node.tagName === 'P'
                            && node.id === 'contents' && node.textContent === text) {
                          let parent = node;
                          while (parent && parent.tagName !== 'BILI-COMMENT-RENDERER')
                            parent = parent.parentElement || parent.getRootNode()?.host;
                          if (parent?.shadowRoot?.querySelector(`a[href*="space.bilibili.com/${uid}"]`))
                            return true;
                        }
                        if (node.shadowRoot) roots.push(node.shadowRoot);
                        roots.push(...node.childNodes);
                      }
                      return false;
                    }""", arg={"text": text, "uid": uid}, timeout=8_000)
                    return remote_comment_id
                except (PlaywrightTimeoutError, RuntimeError):
                    # The anchored comment may not render; try the listing API too.
                    pass
            else:
                page.goto(video_url, wait_until="domcontentloaded", timeout=30_000)
            digest = hashlib.sha256(text.encode()).hexdigest()
            domain = urlparse(resource_url).hostname or ""
            result = page.evaluate("""async ({uid, digest, domain}) => {
              const state=window.__INITIAL_STATE__ || {}; const aid=state.aid || state.videoData?.aid;
              if(!aid) return null;
              for (const sort of [0,2]) for(let pn=1;pn<=5;pn++) {
                const response=await fetch(`https://api.bilibili.com/x/v2/reply?type=1&oid=${aid}&sort=${sort}&pn=${pn}&ps=20`, {credentials:'include', signal:AbortSignal.timeout(10000)});
                if(!response.ok) throw new Error(`comment listing HTTP ${response.status}`);
                const data=await response.json();
                if(data.code!==0) throw new Error(`comment listing API code ${data.code}`);
                for(const r of data.data?.replies || []) {
                  const msg=r.content?.message || '';
                  const hash=await crypto.subtle.digest('SHA-256', new TextEncoder().encode(msg));
                  const hex=[...new Uint8Array(hash)].map(x=>x.toString(16).padStart(2,'0')).join('');
                  if(String(r.mid)===uid && hex===digest && (!domain || msg.includes(domain))) return String(r.rpid);
                }
              } return null;
            }""", {"uid": uid, "digest": digest, "domain": domain})
            return result or None
        finally:
            page.close()
