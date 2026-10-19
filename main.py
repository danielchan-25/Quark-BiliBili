from __future__ import annotations

import argparse
import contextlib
from contextvars import ContextVar
import hashlib
import random
import re
import sqlite3
import sys
import time as task_clock
from datetime import datetime, time, timedelta, timezone

from mediaflow.bilibili import BilibiliClient, Candidate, SubmissionUncertain, CommentPreparationError
from mediaflow.candidate_screening import screen_candidate
from mediaflow.candidate_pool import eligible_count
from mediaflow.account_csv import promotion_keywords, promotion_links
from mediaflow.audit import write_audit_event, audited, audit_step, redact
from mediaflow.dedup import reserve, text_used
from mediaflow.daily_review import daily_review
from mediaflow.config import ROOT, Settings
from mediaflow.database import Database
from mediaflow.delivery import deliver_once
from mediaflow.notify import FeishuNotifier
from mediaflow.toutiao import content_hash, parse_article

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError):
        pass

PROMOTION_SLOTS = ("00:05", "00:41", "01:15", "01:33", "05:06", "05:41", "06:33", "09:15", "10:07", "10:41", "11:33", "15:08", "15:41", "16:33", "17:15", "20:09", "20:41", "21:33")


_prepared_publication = ContextVar('prepared_publication', default=None)


@contextlib.contextmanager
def prepared_publication(db, client, account_id, resource_id, candidate):
    """Pass a live, checked page into promotion without changing its public arguments."""
    token = _prepared_publication.set(dict(db=db, client=client, account_id=account_id,
                                          resource_id=resource_id, candidate=candidate))
    try:
        yield
    finally:
        _prepared_publication.reset(token)


@audited('account_command')
def account(db: Database, args: argparse.Namespace) -> int:
    if args.account_command == "add":
        with BilibiliClient(args.cdp_url) as client:
            uid = client.uid()
        if not uid:
            raise RuntimeError("Bilibili login was not detected; manually log in before adding the account")
        name = f"bilibili-{uid}"
        if args.name and args.name != name:
            raise RuntimeError(f"account names are platform-UID; use {name}")
        db.connection.execute("INSERT INTO accounts(name,platform,role,cdp_url,profile_label,uid) VALUES(?,?,?,?,?,?)", (name, "bilibili", args.role, args.cdp_url, args.profile_label, uid))
        db.connection.commit(); print(f"added account {name}"); return 0
    rows = db.connection.execute("SELECT name,platform,role,cdp_url,profile_label,uid,enabled FROM accounts ORDER BY name").fetchall()
    for row in rows: print(dict(row))
    return 0


def selected_account(db: Database, name: str, role: str):
    row = db.connection.execute("SELECT * FROM accounts WHERE name=? AND role=? AND enabled=1", (name, role)).fetchone()
    if not row: raise RuntimeError(f"enabled {role} account not found: {name}")
    return row


@audited('account_check')
def check_account(db: Database, args: argparse.Namespace) -> int:
    row = db.connection.execute("SELECT * FROM accounts WHERE name=? AND enabled=1", (args.name,)).fetchone()
    if not row: raise RuntimeError(f"account not found: {args.name}")
    with BilibiliClient(row["cdp_url"]) as client: uid = client.uid()
    if not uid: raise RuntimeError("Bilibili login was not detected in this CDP profile")
    canonical_name = f"bilibili-{uid}"
    conflict = db.connection.execute("SELECT id FROM accounts WHERE name=? AND id<>?", (canonical_name,row["id"])).fetchone()
    if conflict: raise RuntimeError(f"cannot normalize account name; {canonical_name} already exists")
    db.connection.execute("UPDATE accounts SET name=?,uid=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (canonical_name,uid,row["id"])); db.connection.commit()
    print(f"account {canonical_name} verified; uid={uid}"); return 0


@audited('resource_command')
def resource(db: Database, args: argparse.Namespace) -> int:
    if args.resource_command == "add":
        db.connection.execute("INSERT INTO promotion_resources(category,title,url,keywords,custom_text) VALUES(?,?,?,?,?)", (args.category,args.title,args.url,args.keywords,args.custom_text))
        db.connection.commit(); print("resource added"); return 0
    for row in db.connection.execute("SELECT id,category,title,url,keywords,enabled FROM promotion_resources ORDER BY id"):
        print(dict(row))
    return 0


@audited('toutiao_command')
def toutiao(db: Database, args: argparse.Namespace) -> int:
    if args.toutiao_command == "account-add":
        name = f"toutiao-{args.uid}"
        existing = db.connection.execute("SELECT id FROM accounts WHERE name=?", (name,)).fetchone()
        if existing:
            db.connection.execute(
                "UPDATE accounts SET cdp_url=?,profile_label=?,enabled=1,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (args.cdp_url, args.profile_label, existing["id"]),
            )
        else:
            db.connection.execute(
                "INSERT INTO accounts(name,platform,role,cdp_url,profile_label,uid) VALUES(?,?,?,?,?,?)",
                (name, "toutiao", "promotion", args.cdp_url, args.profile_label, args.uid),
            )
        db.connection.commit()
        print(f"added account {name}")
        return 0

    account_row = db.connection.execute(
        "SELECT * FROM accounts WHERE name=? AND platform='toutiao' AND enabled=1",
        (args.account,),
    ).fetchone()
    if not account_row:
        raise RuntimeError(f"enabled Toutiao account not found: {args.account}")

    if args.toutiao_command == "record":
        article = parse_article(args.article_url)
        duplicate = db.connection.execute(
            "SELECT id,publish_status FROM promotion_records WHERE account_id=? AND platform='toutiao' AND video_url=?",
            (account_row["id"], article.url),
        ).fetchone()
        if duplicate:
            raise RuntimeError(
                f"article is already recorded for this account (record={duplicate['id']}, status={duplicate['publish_status']})"
            )
        db.connection.execute(
            """INSERT INTO promotion_records(
                account_id,platform,video_url,bvid,target_type,target_id,text,content_hash,
                publish_status,verification_status,detail,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                account_row["id"], "toutiao", article.url, article.article_id,
                "article", article.article_id, args.text.strip(), content_hash(args.text),
                args.status, args.verification, args.detail or "manual_record",
                args.published_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        db.connection.commit()
        print(f"recorded Toutiao comment for article {article.article_id}")
        return 0

    rows = db.connection.execute(
        """SELECT id,target_id,video_url,text,publish_status,verification_status,detail,created_at
           FROM promotion_records WHERE account_id=? AND platform='toutiao'
           ORDER BY created_at DESC,id DESC""",
        (account_row["id"],),
    ).fetchall()
    for row in rows:
        print(dict(row))
    return 0


def rate_ready(db: Database, account_id: int) -> tuple[bool, str]:
    now=datetime.now(); today=now.strftime("%Y-%m-%d")
    rows=db.connection.execute("SELECT created_at FROM promotion_records WHERE account_id=? AND publish_status IN ('published','submitted') ORDER BY created_at DESC", (account_id,)).fetchall()
    daily=sum(1 for r in rows if str(r["created_at"]).startswith(today))
    if daily >= 5: return False, "daily comment limit reached (5/5)"
    if rows:
        last=datetime.fromisoformat(str(rows[0]["created_at"]).replace("Z","+00:00").replace(" ","T"))
        if now-last.replace(tzinfo=None) < timedelta(minutes=15): return False, "minimum 15-minute interval has not elapsed"
    return True, f"daily {daily}/5"


def render_phrase(text: str, category: str, url: str) -> str:
    return text.replace("{category}", category).replace("{url}", url)


@audited('phrase_selection')
def choose_phrase(db: Database, category: str, url: str) -> tuple[str, str, int | None]:
    # Old archived phrases can retain obsolete share links. Only select a
    # phrase that uses the configured placeholder, or contains no URL at all.
    rows=db.connection.execute("""SELECT * FROM phrases
        WHERE category IN (?, '__DEFAULT__')
          AND (text LIKE '%{url}%' OR (text NOT LIKE '%http://%' AND text NOT LIKE '%https://%'))
        ORDER BY last_used_at IS NOT NULL, last_used_at, use_count""", (category,)).fetchall()
    for row in rows:
        text=render_phrase(row["text"],category,url)
        if url not in text:
            text=f"{text.rstrip()}\n{url}"
        if text_used(db, text):
            continue
        write_audit_event(ROOT/'logs/workflow.jsonl', 'phrase_selected', phrase_id=row['id'], source=row['source'])
        return text, row['source'], row['id']
    text = f"整理了一份 {category} 相关资料，有需要可以看看：\n{url}"
    if text_used(db, text):
        raise ValueError('没有未使用的留言文案，请补充话术后再发布')
    write_audit_event(ROOT/'logs/workflow.jsonl', 'phrase_selected', phrase_id=None, source='fallback_template')
    return text, "fallback_template", None


def phrase(db: Database, category: str, url: str) -> tuple[str, str]:
    text, source, phrase_id = choose_phrase(db, category, url)
    if phrase_id is not None:
        db.connection.execute('UPDATE phrases SET use_count=use_count+1,last_used_at=CURRENT_TIMESTAMP WHERE id=?', (phrase_id,))
        db.connection.commit()
    return text, source


def detail_value(detail: str, key: str) -> str:
    marker = f"{key}="
    if marker not in detail:
        return ""
    return detail.split(marker, 1)[1].split(";", 1)[0].strip()


def available_candidate(db: Database, account_id: int, resource_id: int, excluded: tuple[str, ...] = ()) -> Candidate | None:
    exclusion = ' AND candidate.bvid NOT IN (' + ','.join('?' for _ in excluded) + ')' if excluded else ''
    # Older databases may be opened read-only by validation before migration.
    has_cooldowns = db.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='comment_candidate_cooldowns'").fetchone()
    cooldown = ''' AND NOT EXISTS (SELECT 1 FROM comment_candidate_cooldowns cc
        WHERE cc.account_id=? AND cc.bvid=candidate.bvid AND cc.expires_at>CURRENT_TIMESTAMP)''' if has_cooldowns else ''
    has_readiness = db.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='comment_candidate_readiness'").fetchone()
    readiness_order = ("EXISTS (SELECT 1 FROM comment_candidate_readiness cr WHERE cr.account_id=? AND cr.bvid=candidate.bvid AND cr.expires_at>CURRENT_TIMESTAMP) DESC, " if has_readiness else '')
    row = db.connection.execute(
        """SELECT candidate.bvid, candidate.video_url, candidate.title, candidate.author
           FROM promotion_candidates AS candidate
           WHERE candidate.resource_id=? AND candidate.platform='bilibili'
             AND candidate.status='available'
             AND candidate.ai_status='approved'
             AND NOT EXISTS (SELECT 1 FROM historical_comment_videos h WHERE h.bvid=candidate.bvid)
             AND NOT EXISTS (
                 SELECT 1 FROM promotion_records AS record
                 WHERE record.platform='bilibili' AND (record.bvid=candidate.bvid OR record.video_url=candidate.video_url)
             )
             AND NOT EXISTS (SELECT 1 FROM promotion_reservations r WHERE r.bvid=candidate.bvid)
        """ + cooldown + exclusion + """
           ORDER BY """ + readiness_order + """candidate.ai_score DESC, candidate.ai_confidence DESC, candidate.created_at DESC, candidate.id DESC
           LIMIT 1""",
        (resource_id, *((account_id,) if has_cooldowns else ()), *excluded, *((account_id,) if has_readiness else ())),
    ).fetchone()
    if not row:
        return None
    return Candidate(row["bvid"], row["video_url"], row["title"] or "", row["author"] or "")


@audited('candidate_selection')
def available_visible_candidate(db: Database, client: BilibiliClient, account_id: int, resource_id: int, *,
                                mutate: bool = True, retain_page: bool = False,
                                max_candidates: int = 3, budget_seconds: float = 330, max_permission_skips: int = 9, initial_excluded: tuple[str, ...] = ()) -> Candidate | None:
    if not 1 <= max_candidates <= 3 or not 0 < budget_seconds <= 330 or not 0 <= max_permission_skips <= 9:
        raise ValueError('Invalid comment candidate selection limits')
    started = task_clock.monotonic()
    candidate = available_candidate(db, account_id, resource_id, initial_excluded)
    excluded = list(initial_excluded)
    last_error = None
    timeout_videos = []
    permission_skips = 0
    failure_candidates = 0
    while candidate:
        remaining = budget_seconds - (task_clock.monotonic() - started)
        if failure_candidates >= max_candidates or permission_skips >= max_permission_skips > 0 or remaining <= 0:
            write_audit_event(ROOT/'logs/workflow.jsonl', 'candidate_selection_exhausted',
                              account_id=account_id, candidates_checked=len(excluded),
                              elapsed_ms=round((task_clock.monotonic()-started)*1000))
            if last_error:
                raise last_error
            return None
        visible = client.video_visible(candidate.bvid)
        if visible:
            remaining = budget_seconds - (task_clock.monotonic() - started)
            if remaining <= 0:
                write_audit_event(ROOT/'logs/workflow.jsonl', 'candidate_selection_exhausted',
                                  account_id=account_id, candidates_checked=len(excluded), reason='budget_exhausted')
                raise last_error or CommentPreparationError('selection_budget', 0, 'TimeoutError')
            try:
                # Visibility alone does not imply permission to comment. This
                # check never types and runs before consuming a phrase/reserving.
                client.check_comment_editor(candidate.url, retain=retain_page,
                                            budget_seconds=min(90, remaining))
                if mutate:
                    db.connection.execute("""INSERT INTO comment_candidate_readiness(account_id,bvid,expires_at)
                        VALUES(?,?,datetime('now','+6 hours')) ON CONFLICT(account_id,bvid)
                        DO UPDATE SET checked_at=CURRENT_TIMESTAMP,expires_at=excluded.expires_at""", (account_id,candidate.bvid))
                    db.connection.commit()
                break
            except CommentPreparationError as exc:
                disabled = exc.reason in ('comments_disabled_by_author', 'comments_disabled')
                transient = exc.error_type == 'TimeoutError' or exc.stage in ('editor_resolve', 'editor_focus')
                if not disabled and not transient:
                    raise
                if disabled and max_permission_skips:
                    permission_skips += 1
                else:
                    failure_candidates += 1
                last_error = exc if not disabled else last_error
                if len(exc.failures) == 3 and all(f['stage'] == 'comment_section' and f['error_type'] == 'TimeoutError' for f in exc.failures):
                    timeout_videos.append(dict(bvid=candidate.bvid, failures=exc.failures))
                if len(timeout_videos) == 3:
                    exc.reason = 'comment_section_repeated_timeout'
                    exc.timeout_videos = timeout_videos
                    write_audit_event(ROOT/'logs/workflow.jsonl', 'comment_section_repeated_timeout',
                                      account_id=account_id, videos=timeout_videos, total_attempts=9,
                                      diagnosis_required=True)
                cooldown_minutes = 360 if disabled else 30
                if mutate:
                    db.connection.execute('DELETE FROM comment_candidate_readiness WHERE account_id=? AND bvid=?', (account_id,candidate.bvid))
                    db.connection.execute('''INSERT INTO comment_candidate_cooldowns(account_id,bvid,expires_at,stage,error_type)
                        VALUES(?,?,datetime('now',?),?,?) ON CONFLICT(account_id,bvid)
                        DO UPDATE SET expires_at=excluded.expires_at,stage=excluded.stage,
                        error_type=excluded.error_type,updated_at=CURRENT_TIMESTAMP''',
                        (account_id, candidate.bvid, f'+{cooldown_minutes} minutes', exc.stage, exc.error_type))
                    db.connection.commit()
                write_audit_event(ROOT/'logs/workflow.jsonl', 'candidate_comments_disabled' if disabled else 'candidate_preparation_skipped',
                                  account_id=account_id, bvid=candidate.bvid, resource_id=resource_id,
                                  reason=exc.reason, error_stage=exc.stage, error_type=exc.error_type,
                                  cooldown_minutes=cooldown_minutes, persisted=mutate, candidate_attempt=len(excluded)+1)
        else:
            failure_candidates += 1
            write_audit_event(ROOT/'logs/workflow.jsonl', 'candidate_unavailable', bvid=candidate.bvid, resource_id=resource_id, persisted=mutate)
        excluded.append(candidate.bvid)
        # Permission can depend on the account. Do not globally disable a
        # video for other accounts when this account cannot comment on it.
        if mutate and not visible:
            db.connection.execute(
                "UPDATE promotion_candidates SET status='unavailable' WHERE resource_id=? AND platform='bilibili' AND bvid=?",
                (resource_id, candidate.bvid),
            )
            db.connection.commit()
        candidate = available_candidate(db, account_id, resource_id, tuple(excluded))
    if not candidate and last_error:
        # Preserve the actual loading failure for the scheduled alert.
        write_audit_event(ROOT/'logs/workflow.jsonl', 'candidate_selection_exhausted',
                          account_id=account_id, candidates_checked=len(excluded),
                          reason='eligible_candidates_exhausted',
                          elapsed_ms=round((task_clock.monotonic()-started)*1000))
        raise last_error
    write_audit_event(ROOT/'logs/workflow.jsonl', 'candidate_selected', resource_id=resource_id, bvid=candidate.bvid if candidate else None)
    return candidate


@audited('candidate_refresh')
def refresh_promotion_candidates(db: Database, args: argparse.Namespace) -> int:
    settings = Settings.load()
    audit_log = settings.log_dir / "promotion-candidates.jsonl"
    if args.account:
        accounts = [selected_account(db, args.account, "promotion")]
        if accounts[0]["platform"] != "bilibili":
            raise RuntimeError(f"not a BiliBili account: {args.account}")
    else:
        accounts = db.connection.execute(
            "SELECT * FROM accounts WHERE role='promotion' AND platform='bilibili' AND enabled=1 ORDER BY id"
        ).fetchall()
    resources = db.connection.execute(
        "SELECT id,category FROM promotion_resources WHERE enabled=1 ORDER BY id"
    ).fetchall()
    added = refreshed = approved = rejected = 0
    write_audit_event(audit_log, "candidate_refresh_started", accounts=len(accounts), resources=len(resources))
    try:
        for acc in accounts:
            keywords = promotion_keywords(ROOT / "data" / "account.csv", acc["name"])
            write_audit_event(audit_log, "account_refresh_started", account=acc["name"], keywords=keywords)
            with BilibiliClient(acc["cdp_url"]) as client:
                if client.uid() != acc["uid"]:
                    raise RuntimeError(f"CDP profile UID does not match the configured account: {acc['name']}")
                write_audit_event(audit_log, "account_verified", account=acc["name"])
                for keyword in keywords:
                    for candidate in client.search(keyword):
                        if db.connection.execute('SELECT 1 FROM historical_comment_videos WHERE bvid=?', (candidate.bvid,)).fetchone():
                            continue
                        for resource in resources:
                            screening = screen_candidate(
                                api_key=settings.typesafe_api_key,
                                keyword=keyword,
                                resource_category=resource["category"],
                                title=candidate.title,
                                author=candidate.author,
                            )
                            existed = db.connection.execute(
                                "SELECT 1 FROM promotion_candidates WHERE resource_id=? AND platform='bilibili' AND video_url=?",
                                (resource["id"], candidate.url),
                            ).fetchone()
                            db.connection.execute(
                                """INSERT INTO promotion_candidates(
                                    resource_id, platform, video_url, bvid, title, author, relevance, status, note,
                                    ai_score, ai_confidence, ai_risk, ai_status, created_at
                                ) VALUES(?, 'bilibili', ?, ?, ?, ?, ?, 'available', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                                ON CONFLICT(resource_id, platform, video_url) DO UPDATE SET
                                    bvid=excluded.bvid, title=excluded.title, author=excluded.author,
                                    relevance=excluded.relevance, status='available', note=excluded.note,
                                    ai_score=excluded.ai_score, ai_confidence=excluded.ai_confidence,
                                    ai_risk=excluded.ai_risk, ai_status=excluded.ai_status,
                                    created_at=CURRENT_TIMESTAMP""",
                                (
                                    resource["id"], candidate.url, candidate.bvid, candidate.title, candidate.author,
                                    keyword, f"refreshed_by={acc['name']}", screening.relevance_score,
                                    screening.confidence, screening.risk_probability, screening.status,
                                ),
                            )
                            write_audit_event(
                                audit_log,
                                "candidate_screened",
                                account=acc["name"], keyword=keyword, resource_id=resource["id"],
                                resource_category=resource["category"], bvid=candidate.bvid,
                                video_url=candidate.url, title=candidate.title, author=candidate.author,
                                existed=bool(existed), relevance_score=screening.relevance_score,
                                confidence=screening.confidence, risk_probability=screening.risk_probability,
                                status=screening.status,
                            )
                            if screening.status == "approved":
                                approved += 1
                            else:
                                rejected += 1
                            if existed:
                                refreshed += 1
                            else:
                                added += 1
            db.connection.commit()
    except Exception as exc:
        db.connection.rollback()
        write_audit_event(audit_log, "candidate_refresh_failed", error_type=type(exc).__name__)
        raise
    write_audit_event(
        audit_log,
        "candidate_refresh_completed",
        added=added, refreshed=refreshed, approved=approved, rejected=rejected,
    )
    print(f"promotion candidates refreshed: added={added} refreshed={refreshed} approved={approved} rejected={rejected}")
    return 0


@audited('candidate_pool_replenishment', 'candidate-pool.jsonl')
def replenish_promotion_candidates(db, account, policy, target, report):
    """Search beyond the first page and checkpoint each successful judgment."""
    if db.connection.in_transaction:
        raise RuntimeError('Candidate replenishment requires a clean transaction')
    settings = Settings.load()
    acc = selected_account(db, account, 'promotion')
    if acc['platform'] != 'bilibili':
        raise ValueError('Candidate replenishment requires a BiliBili account')
    keywords = promotion_keywords(ROOT/'data/account.csv', account)
    resources = db.connection.execute('SELECT id,category FROM promotion_resources WHERE enabled=1 ORDER BY id').fetchall()
    report.update(pages=0, reviewed=0, approved=0, rejected=0, added=0,
                  skipped_used=0, skipped_known=0, skipped_unavailable=0, reason='search_exhausted')
    deadline = task_clock.monotonic() + policy.max_seconds
    seen, visibility = set(), {}
    log = ROOT/'logs/candidate-pool.jsonl'

    def needed():
        return [resource for resource in resources if eligible_count(db, resource['id']) < target]

    def budget_ready():
        if task_clock.monotonic() >= deadline:
            report['reason'] = 'elapsed_budget_reached'
            return False
        if report['reviewed'] >= policy.max_reviews:
            report['reason'] = 'review_budget_reached'
            return False
        return True

    with BilibiliClient(acc['cdp_url']) as client:
        if client.uid() != acc['uid']:
            raise RuntimeError('Candidate replenishment browser UID mismatch')
        for page_number in range(1, policy.pages_per_keyword + 1):
            for keyword_index, keyword in enumerate(keywords):
                if not needed():
                    report['reason'] = 'target_reached'
                    return
                if not budget_ready():
                    return
                with audit_step('candidate_pool_search', 'candidate-pool.jsonl',
                                account=account, page_number=page_number, keyword_index=keyword_index):
                    candidates = client.search(keyword, limit=policy.page_size, page_number=page_number)
                report['pages'] += 1
                for candidate in candidates:
                    if not needed():
                        report['reason'] = 'target_reached'
                        return
                    used = db.connection.execute('''SELECT 1 FROM promotion_records
                        WHERE platform='bilibili' AND (bvid=? OR video_url=?)
                        UNION ALL SELECT 1 FROM promotion_reservations WHERE bvid=?
                        UNION ALL SELECT 1 FROM historical_comment_videos WHERE bvid=? LIMIT 1''',
                        (candidate.bvid, candidate.url, candidate.bvid, candidate.bvid)).fetchone()
                    if used:
                        report['skipped_used'] += 1
                        continue
                    for resource in needed():
                        key = (resource['id'], candidate.bvid, keyword)
                        if key in seen:
                            report['skipped_known'] += 1
                            continue
                        seen.add(key)
                        existing = db.connection.execute('''SELECT * FROM promotion_candidates
                            WHERE resource_id=? AND platform='bilibili' AND (bvid=? OR video_url=?)
                            ORDER BY id DESC LIMIT 1''', (resource['id'], candidate.bvid, candidate.url)).fetchone()
                        unchanged = existing and (existing['title'] or '') == candidate.title and (existing['author'] or '') == candidate.author
                        recent_rejection = False
                        if unchanged and existing['ai_status'] == 'rejected' and existing['relevance'] == keyword:
                            try:
                                created = datetime.fromisoformat(existing['created_at'])
                                if created.tzinfo is None:
                                    created = created.replace(tzinfo=timezone.utc)
                                age = datetime.now(timezone.utc) - created
                                recent_rejection = 0 <= age.total_seconds() < 86400
                            except (ValueError, TypeError):
                                pass
                        if unchanged and ((existing['status'] == 'available' and existing['ai_status'] == 'approved') or recent_rejection):
                            report['skipped_known'] += 1
                            continue
                        if not budget_ready():
                            return
                        if candidate.bvid not in visibility:
                            visibility[candidate.bvid] = client.video_visible(candidate.bvid)
                        if not visibility[candidate.bvid]:
                            report['skipped_unavailable'] += 1
                            continue
                        report['reviewed'] += 1
                        screening = screen_candidate(api_key=settings.typesafe_api_key, keyword=keyword,
                            resource_category=resource['category'], title=candidate.title, author=candidate.author)
                        if screening.status not in ('approved', 'rejected'):
                            raise ValueError('Invalid candidate screening status')
                        # Commit only this judgment. An error on a later video
                        # cannot roll back candidates already approved and saved.
                        with db.connection:
                            db.connection.execute('''INSERT INTO promotion_candidates(
                                resource_id,platform,video_url,bvid,title,author,relevance,status,note,
                                ai_score,ai_confidence,ai_risk,ai_status,created_at)
                                VALUES(?,'bilibili',?,?,?,?,?,'available',?,?,?,?,?,CURRENT_TIMESTAMP)
                                ON CONFLICT(resource_id,platform,video_url) DO UPDATE SET
                                bvid=excluded.bvid,title=excluded.title,author=excluded.author,
                                relevance=excluded.relevance,status='available',note=excluded.note,
                                ai_score=excluded.ai_score,ai_confidence=excluded.ai_confidence,
                                ai_risk=excluded.ai_risk,ai_status=excluded.ai_status,created_at=CURRENT_TIMESTAMP''',
                                (resource['id'], candidate.url, candidate.bvid, candidate.title, candidate.author,
                                 keyword, f'refilled_by={account}', screening.relevance_score, screening.confidence,
                                 screening.risk_probability, screening.status))
                        report[screening.status] += 1
                        report['added'] += not bool(existing)
                        write_audit_event(log, 'candidate_pool_judgment_saved', account=account,
                            resource_id=resource['id'], bvid=candidate.bvid, status=screening.status,
                            eligible=eligible_count(db, resource['id']))
        if not needed():
            report['reason'] = 'target_reached'


@audited('legacy_task_summary')
def promotion_summary(db: Database, notify: FeishuNotifier, args: argparse.Namespace) -> int:
    day = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else datetime.now().date()
    # SQLite CURRENT_TIMESTAMP is UTC while schedules and notifications use
    # China Standard Time. Convert the local calendar-day bounds to UTC before
    # querying, so the 00:00–07:59 local slots stay in the correct daily report.
    local_start = datetime.combine(day, time.min)
    utc_start = local_start - timedelta(hours=8)
    utc_end = utc_start + timedelta(days=1)
    rows = db.connection.execute(
        """SELECT a.name, a.platform, tr.status, tr.detail, tr.started_at
           FROM task_runs AS tr
           JOIN accounts AS a ON a.id = tr.account_id
           WHERE tr.task_type='promotion' AND tr.started_at >= ? AND tr.started_at < ?
             AND COALESCE(tr.detail,'') NOT LIKE '%reason=dry-run%'
           ORDER BY tr.started_at, tr.id""",
        (utc_start.strftime("%Y-%m-%d %H:%M:%S"), utc_end.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchall()
    labels = {"SUCCESS": "成功"}
    successes = sum(row["status"] == "SUCCESS" for row in rows)
    failures = len(rows) - successes
    lines = ["【MediaProject】【推广】", ""]
    for row in rows:
        detail = row["detail"] or ""
        slot = detail_value(detail, "slot") or (datetime.fromisoformat(row['started_at']) + timedelta(hours=8)).strftime('%H:%M')
        platform = {"bilibili": "BiliBili", "toutiao": "今日头条"}.get(row["platform"], row["platform"])
        line = f"- {slot}: 平台={platform}, 账户={row['name']}, {labels.get(row['status'], '失败')}"
        reason = detail_value(detail, "reason")
        if row['status'] != 'SUCCESS':
            reason = reason or {'RUNNING': '任务未正常结束，结果待核查', 'PENDING': '提交待复核，禁止自动重发', 'SKIPPED': '未执行发布'}.get(row['status'], '未完成发布')
            line += f"，失败原因：{reason}"
        lines.append(line)
    lines.extend(["", f"今日汇总: 成功{successes}, 失败{failures}", "推广链接：", ""])
    links = promotion_links(ROOT / "data" / "account.csv")
    lines.extend(f"- [{link}]({link})" for link in links)
    notify.send_text("\n".join(lines))
    print(f"promotion summary sent: date={day.isoformat()} success={successes} failed={failures}")
    return 0


@audited('promotion')
def promotion(db: Database, notify: FeishuNotifier, args: argparse.Namespace) -> int:
    acc = selected_account(db, args.account, 'promotion')
    if getattr(args, 'max_comments', 1) != 1:
        raise ValueError('Each promotion run must allow exactly one comment')
    if getattr(args, 'dry_run', False):
        return _promotion(db, notify, args, None)
    # Fail closed even if the process is killed before Python can run finally.
    run_id = db.connection.execute("INSERT INTO task_runs(account_id,task_type,status,detail) VALUES(?,'promotion','FAILED',?)", (acc['id'], f'slot={args.slot or ""}; reason=任务未完成，结果待核查')).lastrowid
    db.connection.commit()
    try:
        return _promotion(db, notify, args, run_id)
    except BaseException as exc:
        db.connection.rollback()
        db.connection.execute("UPDATE task_runs SET status='FAILED',detail=? WHERE id=?", (f'slot={args.slot or ""}; reason={type(exc).__name__}: {redact(str(exc))[:1000]}', run_id))
        db.connection.commit()
        raise
    finally:
        db.connection.execute('UPDATE task_runs SET ended_at=CURRENT_TIMESTAMP WHERE id=? AND ended_at IS NULL', (run_id,))
        db.connection.commit()

        result = db.connection.execute('SELECT status,detail,started_at FROM task_runs WHERE id=?', (run_id,)).fetchone()
        if result['status'] != 'SUCCESS' and not getattr(args, 'dry_run', False) and notify is not None:
            try:
                if not notify.webhook:
                    raise RuntimeError('FEISHU_WEBHOOK not configured')
                clock = (datetime.fromisoformat(result['started_at']) + timedelta(hours=8)).strftime('%H:%M')
                reason = (detail_value(result['detail'] or '', 'reason') or result['detail'] or '发布未完成').replace('\r', ' ').replace('\n', ' ')
                deliver_once(ROOT/'data/notification-receipts'/f'promotion-{run_id}.json', notify,
                             f"【Quark-BiliBili】\n\n- {clock}: 平台=BiliBili, 账户={acc['name']}, 失败，原因={redact(reason)}",
                             kind='promotion_failure', run_id=run_id, account=acc['name'])
                write_audit_event(ROOT/'logs/promotion-alerts.jsonl','alert_sent',run_id=run_id,account=acc['name'])
            except Exception as alert_error:
                write_audit_event(ROOT/'logs/promotion-alerts.jsonl','alert_failed',run_id=run_id,account=acc['name'],error_type=type(alert_error).__name__)
                print(f'ALERT FAILED: {type(alert_error).__name__}; see logs/promotion-alerts.jsonl',file=sys.stderr)


def _promotion(db: Database, notify: FeishuNotifier, args: argparse.Namespace, run_id: int | None) -> int:
    acc = selected_account(db, args.account, "promotion")
    log = ROOT / 'logs/promotion-flow.jsonl'

    def event(name, **fields):
        write_audit_event(log, name, run_id=run_id, account=acc['name'], dry_run=args.dry_run, **fields)

    def finish(status, reason='', video_url=''):
        event('promotion_result', status=status, reason=reason)
        if run_id is not None:
            db.connection.execute("UPDATE task_runs SET status=?,detail=?,ended_at=CURRENT_TIMESTAMP WHERE id=?",
                                  (status, f"slot={args.slot or ''}; reason={reason}; url={video_url}", run_id))
            db.connection.commit()

    if not acc['uid']:
        raise RuntimeError('Verify the account UID before promotion')
    ready, detail = rate_ready(db, acc['id'])
    event('frequency_checked', ready=ready, reason=detail)
    if not ready:
        finish('SKIPPED', detail)
        print(detail)
        return 1
    resources = db.connection.execute('SELECT * FROM promotion_resources WHERE enabled=1 ORDER BY id').fetchall()
    keywords = promotion_keywords(ROOT / 'data/account.csv', acc['name'])
    event('promotion_configuration_checked', resources=len(resources), keywords=len(keywords))
    successes = failures = pending = skips = 0
    reasons = []
    latest_url = ''
    prepared = _prepared_publication.get()
    if prepared and (prepared['db'] is not db or prepared['account_id'] != acc['id']):
        raise RuntimeError('Prepared publication account or database mismatch')
    with (contextlib.nullcontext(prepared['client']) if prepared else BilibiliClient(acc['cdp_url'])) as client:
        if client.uid() != acc['uid']:
            raise RuntimeError('CDP profile UID does not match the configured account')
        event('promotion_identity_verified')
        for resource in resources:
            if successes + failures + pending >= 1:
                break
            if (prepared and prepared['resource_id'] == resource['id']
                    and available_candidate(db, acc['id'], resource['id']) == prepared['candidate']):
                candidate = prepared['candidate']
                event('prepared_candidate_reused', bvid=candidate.bvid)
            else:
                candidate = available_visible_candidate(db, client, acc['id'], resource['id'],
                                                        mutate=not args.dry_run, retain_page=not args.dry_run)
            if not candidate:
                skips += 1
                event('resource_skipped', resource_id=resource['id'], reason='no_candidate')
                continue
            latest_url = candidate.url
            try:
                text, source, phrase_id = choose_phrase(db, resource['category'], resource['url'])
            except ValueError:
                event('phrase_exhausted', resource_id=resource['id'])
                skips += 1
                reasons.append('没有未使用的留言文案')
                continue
            if args.dry_run:
                event('preview_ready', bvid=candidate.bvid, resource_id=resource['id'], source=source)
                print(f"DRY-RUN target={candidate.url}\nsource={source}\ntext:\n{text}")
                finish('DRY_RUN', 'dry-run', candidate.url)
                return 0
            try:
                with audit_step('comment_reservation', 'promotion-flow.jsonl', run_id=run_id, bvid=candidate.bvid):
                    reserve(db, candidate, text, acc['id'])
            except (ValueError, sqlite3.IntegrityError) as exc:
                skips += 1
                reasons.append('视频或文案已被使用')
                event('dedup_skipped', bvid=candidate.bvid, error_type=type(exc).__name__)
                continue
            if phrase_id is not None:
                db.connection.execute('UPDATE phrases SET use_count=use_count+1,last_used_at=CURRENT_TIMESTAMP WHERE id=?', (phrase_id,))
            candidate_row = db.connection.execute('SELECT id FROM promotion_candidates WHERE resource_id=? AND video_url=?', (resource['id'], candidate.url)).fetchone()
            # Persist intent before touching the submit button. A crash must remain
            # pending and count against the account limits, never invite a retry.
            record_id = db.connection.execute("""INSERT INTO promotion_records(
                account_id,resource_id,candidate_id,platform,video_url,bvid,text,content_hash,
                publish_status,verification_status,detail,created_at)
                VALUES(?,?,?,'bilibili',?,?,?,?,'submitted','submission_started',?,?)""",
                (acc['id'],resource['id'],candidate_row['id'] if candidate_row else None,candidate.url,candidate.bvid,
                 text,hashlib.sha256(text.encode()).hexdigest(),f'source={source}',datetime.now().strftime('%Y-%m-%d %H:%M:%S'))).lastrowid
            db.connection.commit()
            event('submission_intent_saved', record_id=record_id, bvid=candidate.bvid)
            remote = None
            try:
                submission = client.publish_comment(candidate.url, text)
                status = 'published' if submission == 'network_confirmed' else 'submitted'
                verification = 'accepted_by_api' if status == 'published' else 'pending'
                accepted_id = getattr(client, 'last_submitted_comment_id', None)
                if status == 'published' and isinstance(accepted_id, str) and re.fullmatch(r'[1-9][0-9]*', accepted_id):
                    remote = accepted_id
            except SubmissionUncertain as exc:
                status, verification = 'submitted', 'pending'
                event('submission_uncertain', record_id=record_id, error_type=type(exc).__name__)
            except Exception as exc:
                status, verification = 'failed', 'not_run'
                preparation = isinstance(exc, CommentPreparationError)
                event('submission_failed', record_id=record_id, error_type=type(exc).__name__,
                      error_stage=exc.stage if preparation else None,
                      preparation_attempts=exc.attempts if preparation else None)
                reason = f'{type(exc).__name__}: {exc}' if preparation else type(exc).__name__
                reasons.append(reason)
                db.connection.execute('UPDATE promotion_records SET detail=detail || ? WHERE id=?',
                                      (f'; failure={reason}', record_id))
            # Preserve the platform's accepted comment ID before read-only
            # verification. A lookup miss or exception must not erase it.
            db.connection.execute('UPDATE promotion_records SET publish_status=?,verification_status=?,remote_comment_id=? WHERE id=?', (status,verification,remote,record_id))
            db.connection.commit()
            event('submission_result_saved', record_id=record_id, status=status, verification=verification)
            if status != 'failed':
                try:
                    verified_remote = client.verify_comment(candidate.bvid, acc['uid'], text, resource['url'], remote)
                    if verified_remote:
                        remote = verified_remote
                        status, verification = 'published', 'verified'
                except Exception as exc:
                    # Verification failure does not revoke a successful submit.
                    event('post_submit_verification_failed', record_id=record_id, error_type=type(exc).__name__)
                    verification = 'accepted_by_api_recheck_error' if status == 'published' else 'pending_recheck_error'
                    if status == 'published' and notify is not None:
                        try:
                            deliver_once(ROOT/'data/notification-receipts'/f'verification-{record_id}.json', notify,
                                         f"【Quark-BiliBili】\n\n账户={acc['name']}，评论接口已接受，但后续核验异常：{type(exc).__name__}。禁止重复发布。",
                                         kind='verification_error', record_id=record_id, account=acc['name'])
                        except Exception as alert_error:
                            event('verification_alert_failed', record_id=record_id, error_type=type(alert_error).__name__)
                db.connection.execute('UPDATE promotion_records SET publish_status=?,verification_status=?,remote_comment_id=? WHERE id=?', (status,verification,remote,record_id))
                db.connection.commit()
                event('verification_result_saved', record_id=record_id, status=status, verification=verification, found=bool(remote))
            successes += status == 'published'
            failures += status == 'failed'
            pending += status == 'submitted'
    status = 'SUCCESS' if successes else ('PENDING' if pending else ('FAILED' if failures else 'SKIPPED'))
    reason = '' if successes else ('评论提交待复核，禁止自动重发' if pending else '；'.join(reasons) or '没有可发布的候选或文案')
    finish(status, reason, latest_url)
    print(f'promotion complete: success={successes} pending={pending} failed={failures} skipped={skips}')
    return 0 if successes else 1


@audited('nurture')
def nurture(db: Database, notify: FeishuNotifier, args: argparse.Namespace) -> int:
    acc=selected_account(db,args.account,"nurture")
    with BilibiliClient(acc["cdp_url"]) as client:
        if not client.uid(): raise RuntimeError("Bilibili login was not detected")
        page=client.page()
        try:
            page.goto("https://www.bilibili.com/",wait_until="domcontentloaded",timeout=30_000); page.wait_for_timeout(2_000)
            links=page.locator("a[href*='/video/BV']").evaluate_all("els => [...new Set(els.map(x=>x.href))].slice(0,30)")
        finally: page.close()
        done=0
        like_attempts=0
        for url in links:
            if done>=args.max_videos: break
            bvid=url.split("/video/")[-1].split("?")[0].split("/")[0]
            if db.connection.execute("SELECT 1 FROM videos WHERE account_id=? AND bvid=?",(acc["id"],bvid)).fetchone():
                write_audit_event(ROOT/'logs/workflow.jsonl', 'nurture_video_skipped', account=acc['name'], bvid=bvid, reason='already_recorded')
                continue
            try:
                # Each nurture run watches five successful videos by default and
                # attempts likes on the first three successful watches.
                should_like = like_attempts < 3
                write_audit_event(ROOT/'logs/workflow.jsonl', 'nurture_video_started', account=acc['name'], bvid=bvid, allow_like=should_like)
                duration,watched,liked=client.watch_and_like(url,allow_like=should_like)
                db.connection.execute("INSERT INTO videos(account_id,bvid,url,status,duration,watched_seconds,watched_at) VALUES(?,?,?,'watched',?,?,CURRENT_TIMESTAMP)",(acc["id"],bvid,url,duration,watched));
                if liked: db.connection.execute("INSERT OR IGNORE INTO interactions(account_id,bvid,action) VALUES(?,?,'like')",(acc["id"],bvid))
                if should_like: like_attempts+=1
                db.connection.commit(); done+=1
                write_audit_event(ROOT/'logs/workflow.jsonl', 'nurture_video_completed', account=acc['name'], bvid=bvid, watched_seconds=watched, liked=liked)
            except Exception as exc:
                db.connection.execute("INSERT OR REPLACE INTO videos(account_id,bvid,url,status,last_error) VALUES(?,?,?,'failed',?)",(acc["id"],bvid,url,redact(str(exc))[:1000]))
                db.connection.commit()
                write_audit_event(ROOT/'logs/workflow.jsonl', 'nurture_video_failed', account=acc['name'], bvid=bvid, error_type=type(exc).__name__)
    notify.send("养号任务完成",f"账号：{acc['name']}\n成功观看：{done}\n点赞尝试：{like_attempts}"); print(f"nurture complete: watched={done} like_attempts={like_attempts}"); return 0


@audited('command')
def main() -> int:
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="command",required=True)
    a=sub.add_parser("account"); a_sub=a.add_subparsers(dest="account_command",required=True); add=a_sub.add_parser("add"); add.add_argument("--name",help="optional validation only; name is always platform-UID"); add.add_argument("--role",choices=["promotion","nurture"],required=True); add.add_argument("--cdp-url",required=True); add.add_argument("--profile-label"); a_sub.add_parser("list")
    c=sub.add_parser("account-check"); c.add_argument("--name",required=True)
    r=sub.add_parser("resource"); r_sub=r.add_subparsers(dest="resource_command",required=True); r_add=r_sub.add_parser("add"); r_add.add_argument("--category",required=True); r_add.add_argument("--url",required=True); r_add.add_argument("--keywords",required=True); r_add.add_argument("--title"); r_add.add_argument("--custom-text"); r_sub.add_parser("list")
    tt=sub.add_parser("toutiao"); tt_sub=tt.add_subparsers(dest="toutiao_command",required=True)
    tt_account=tt_sub.add_parser("account-add"); tt_account.add_argument("--uid",required=True); tt_account.add_argument("--cdp-url",required=True); tt_account.add_argument("--profile-label")
    tt_record=tt_sub.add_parser("record"); tt_record.add_argument("--account",required=True); tt_record.add_argument("--article-url",required=True); tt_record.add_argument("--text",required=True); tt_record.add_argument("--status",choices=("published","submitted","failed"),default="published"); tt_record.add_argument("--verification",choices=("verified","ui_confirmed","pending","not_run"),default="verified"); tt_record.add_argument("--detail"); tt_record.add_argument("--published-at")
    tt_list=tt_sub.add_parser("list"); tt_list.add_argument("--account",required=True)
    pr=sub.add_parser("promotion"); pr.add_argument("--account",required=True); pr.add_argument("--max-comments",type=int,default=1); pr.add_argument("--dry-run",action="store_true"); pr.add_argument("--slot",choices=PROMOTION_SLOTS)
    pc=sub.add_parser("promotion-candidates"); pc.add_argument("--account",help="refresh one enabled BiliBili promotion account")
    ps=sub.add_parser("promotion-summary"); ps.add_argument("--date", help="YYYY-MM-DD; defaults to today")
    n=sub.add_parser("nurture"); n.add_argument("--account",required=True); n.add_argument("--max-videos",type=int,default=5)
    args=p.parse_args(); settings=Settings.load(); db=Database(settings.database_path); notify=FeishuNotifier(settings.feishu_webhook,settings.feishu_secret)
    try:
        if args.command=="account": return account(db,args)
        if args.command=="account-check": return check_account(db,args)
        if args.command=="resource": return resource(db,args)
        if args.command=="toutiao": return toutiao(db,args)
        if args.command=="promotion": return promotion(db,notify,args)
        if args.command=="promotion-candidates": return refresh_promotion_candidates(db,args)
        if args.command=="promotion-summary": return daily_review(db,notify,args)
        return nurture(db,notify,args)
    finally: db.close()

if __name__=="__main__":
    try: raise SystemExit(main())
    except Exception as exc: print(f"ERROR: {exc}",file=sys.stderr); raise SystemExit(1)
