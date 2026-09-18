from __future__ import annotations

import hashlib
import json
import random
import re
import time
from dataclasses import dataclass
from urllib.parse import quote, urlparse

from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


_BVID = re.compile(r"/video/(BV[\w]+)")


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
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url, timeout=30_000)
        if not self._browser.contexts:
            raise RuntimeError("CDP browser has no available context")
        self.context = self._browser.contexts[0]
        return self

    def __exit__(self, *_: object) -> None:
        # A CDP connection must only detach; never close the user-managed browser.
        if self._pw:
            self._pw.stop()
        self.context = None

    def page(self) -> Page:
        if not self.context:
            raise RuntimeError("CDP client is not connected")
        return self.context.new_page()

    def uid(self) -> str | None:
        page = self.page()
        try:
            page.goto("https://www.bilibili.com/", wait_until="domcontentloaded", timeout=30_000)
            value = page.evaluate("""() => fetch('https://api.bilibili.com/x/web-interface/nav', {credentials:'include'})
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
            page.locator("#commentapp, bili-comments").first.scroll_into_view_if_needed(timeout=10_000)
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
                page.locator("#commentapp, bili-comments").first.scroll_into_view_if_needed(timeout=10_000)
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

    def verify_comment(self, bvid: str, uid: str, text: str, resource_url: str) -> str | None:
        page = self.page()
        try:
            page.goto(f"https://www.bilibili.com/video/{bvid}/", wait_until="domcontentloaded", timeout=30_000)
            digest = hashlib.sha256(text.encode()).hexdigest()
            domain = urlparse(resource_url).hostname or ""
            result = page.evaluate("""async ({uid, digest, domain}) => {
              const state=window.__INITIAL_STATE__ || {}; const aid=state.aid || state.videoData?.aid;
              if(!aid) return null;
              for (const sort of [0,2]) for(let pn=1;pn<=5;pn++) {
                const data=await fetch(`https://api.bilibili.com/x/v2/reply?type=1&oid=${aid}&sort=${sort}&pn=${pn}&ps=20`, {credentials:'include'}).then(r=>r.json());
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
