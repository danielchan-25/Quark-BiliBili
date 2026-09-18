from __future__ import annotations

import argparse
import hashlib
import random
import sys
from datetime import datetime, time, timedelta

from mediaflow.bilibili import BilibiliClient, Candidate
from mediaflow.account_csv import promotion_keywords, promotion_links
from mediaflow.config import ROOT, Settings
from mediaflow.database import Database
from mediaflow.notify import FeishuNotifier
from mediaflow.toutiao import content_hash, parse_article

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError):
        pass

PROMOTION_SLOTS = ("00:05", "01:15", "05:06", "09:15", "10:07", "15:08", "17:15", "20:09")


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


def resource(db: Database, args: argparse.Namespace) -> int:
    if args.resource_command == "add":
        db.connection.execute("INSERT INTO promotion_resources(category,title,url,keywords,custom_text) VALUES(?,?,?,?,?)", (args.category,args.title,args.url,args.keywords,args.custom_text))
        db.connection.commit(); print("resource added"); return 0
    for row in db.connection.execute("SELECT id,category,title,url,keywords,enabled FROM promotion_resources ORDER BY id"):
        print(dict(row))
    return 0


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


def phrase(db: Database, category: str, url: str) -> tuple[str, str]:
    # Old archived phrases can retain obsolete share links. Only select a
    # phrase that uses the configured placeholder, or contains no URL at all.
    row=db.connection.execute("""SELECT * FROM phrases
        WHERE category IN (?, '__DEFAULT__')
          AND (text LIKE '%{url}%' OR (text NOT LIKE '%http://%' AND text NOT LIKE '%https://%'))
        ORDER BY last_used_at IS NOT NULL, last_used_at, use_count LIMIT 1""", (category,)).fetchone()
    if row:
        text=render_phrase(row["text"],category,url)
        if url not in text:
            text=f"{text.rstrip()}\n{url}"
        db.connection.execute("UPDATE phrases SET use_count=use_count+1,last_used_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],)); db.connection.commit(); return text, row["source"]
    return f"整理了一份 {category} 相关资料，有需要可以看看：\n{url}", "fallback_template"


def detail_value(detail: str, key: str) -> str:
    marker = f"{key}="
    if marker not in detail:
        return ""
    return detail.split(marker, 1)[1].split(";", 1)[0].strip()


def available_candidate(db: Database, account_id: int, resource_id: int) -> Candidate | None:
    row = db.connection.execute(
        """SELECT candidate.bvid, candidate.video_url, candidate.title, candidate.author
           FROM promotion_candidates AS candidate
           WHERE candidate.resource_id=? AND candidate.platform='bilibili'
             AND candidate.status='available'
             AND NOT EXISTS (
                 SELECT 1 FROM promotion_records AS record
                 WHERE record.account_id=? AND record.video_url=candidate.video_url
             )
           ORDER BY candidate.created_at DESC, candidate.id DESC
           LIMIT 1""",
        (resource_id, account_id),
    ).fetchone()
    if not row:
        return None
    return Candidate(row["bvid"], row["video_url"], row["title"] or "", row["author"] or "")


def refresh_promotion_candidates(db: Database, args: argparse.Namespace) -> int:
    accounts = db.connection.execute(
        "SELECT * FROM accounts WHERE role='promotion' AND platform='bilibili' AND enabled=1 ORDER BY id"
    ).fetchall()
    resources = db.connection.execute(
        "SELECT id FROM promotion_resources WHERE enabled=1 ORDER BY id"
    ).fetchall()
    added = refreshed = 0
    for acc in accounts:
        keywords = promotion_keywords(ROOT / "data" / "account.csv", acc["name"])
        with BilibiliClient(acc["cdp_url"]) as client:
            if client.uid() != acc["uid"]:
                raise RuntimeError(f"CDP profile UID does not match the configured account: {acc['name']}")
            for keyword in keywords:
                for candidate in client.search(keyword):
                    for resource in resources:
                        existed = db.connection.execute(
                            "SELECT 1 FROM promotion_candidates WHERE resource_id=? AND platform='bilibili' AND video_url=?",
                            (resource["id"], candidate.url),
                        ).fetchone()
                        db.connection.execute(
                            """INSERT INTO promotion_candidates(
                                resource_id, platform, video_url, bvid, title, author, relevance, status, note, created_at
                            ) VALUES(?, 'bilibili', ?, ?, ?, ?, ?, 'available', ?, CURRENT_TIMESTAMP)
                            ON CONFLICT(resource_id, platform, video_url) DO UPDATE SET
                                bvid=excluded.bvid, title=excluded.title, author=excluded.author,
                                relevance=excluded.relevance, status='available', note=excluded.note,
                                created_at=CURRENT_TIMESTAMP""",
                            (resource["id"], candidate.url, candidate.bvid, candidate.title, candidate.author, keyword, f"refreshed_by={acc['name']}"),
                        )
                        if existed:
                            refreshed += 1
                        else:
                            added += 1
        db.connection.commit()
    print(f"promotion candidates refreshed: added={added} refreshed={refreshed}")
    return 0


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
           ORDER BY tr.started_at, tr.id""",
        (utc_start.strftime("%Y-%m-%d %H:%M:%S"), utc_end.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchall()
    labels = {"SUCCESS": "成功", "FAILED": "失败", "PENDING": "待复核", "SKIPPED": "跳过"}
    successes = sum(row["status"] == "SUCCESS" for row in rows)
    failures = sum(row["status"] == "FAILED" for row in rows)
    lines = ["【MediaProject】【推广】", ""]
    for row in rows:
        detail = row["detail"] or ""
        slot = detail_value(detail, "slot") or str(row["started_at"])[11:16]
        platform = {"bilibili": "BiliBili", "toutiao": "今日头条"}.get(row["platform"], row["platform"])
        line = f"- {slot}: 平台={platform}, 账户={row['name']}, {labels.get(row['status'], row['status'])}"
        reason = detail_value(detail, "reason")
        if row["status"] == "FAILED" and reason:
            line += f"，失败原因：{reason}"
        lines.append(line)
    lines.extend(["", f"今日汇总: 成功{successes}, 失败{failures}", "推广链接：", ""])
    links = promotion_links(ROOT / "data" / "account.csv")
    lines.extend(f"- [{link}]({link})" for link in links)
    notify.send_text("\n".join(lines))
    print(f"promotion summary sent: date={day.isoformat()} success={successes} failed={failures}")
    return 0


def promotion(db: Database, notify: FeishuNotifier, args: argparse.Namespace) -> int:
    acc=selected_account(db,args.account,"promotion")
    run_id = db.connection.execute("INSERT INTO task_runs(account_id,task_type,status,detail) VALUES(?,?,?,?)", (acc["id"],"promotion","RUNNING",f"slot={args.slot or ''}")).lastrowid
    db.connection.commit()
    def finish(status: str, reason: str = "", video_url: str = "") -> None:
        db.connection.execute("UPDATE task_runs SET status=?,detail=detail || ?,ended_at=CURRENT_TIMESTAMP WHERE id=?", (status, f"; reason={reason}; url={video_url}", run_id))
        db.connection.commit()
    if not acc["uid"]: raise RuntimeError("run account check after manually logging in before promotion")
    ready, detail=rate_ready(db,acc["id"])
    if not ready:
        finish("SKIPPED",detail)
        print(detail); return 0
    resources=db.connection.execute("SELECT * FROM promotion_resources WHERE enabled=1 ORDER BY id").fetchall()
    keywords = promotion_keywords(ROOT / "data" / "account.csv", acc["name"])
    successes=failures=pending=skips=0
    failure_reasons: list[str] = []
    with BilibiliClient(acc["cdp_url"]) as client:
        if client.uid()!=acc["uid"]: raise RuntimeError("CDP profile UID does not match the configured account")
        for resource in resources:
            # A run defaults to one comment. This enforces the 15-minute interval
            # across scheduled invocations instead of emitting a burst in one run.
            if successes + failures + pending >= args.max_comments: break
            keyword=random.choice(keywords)
            # Candidates are collected once daily before promotion begins.
            # They remain reusable across accounts but never twice by one account.
            candidate=available_candidate(db, acc["id"], resource["id"])
            if not candidate:
                failures += 1
                failure_reasons.append(f"候选池为空（关键词={keyword}）")
                continue
            if args.dry_run:
                preview=db.connection.execute("""SELECT text,source FROM phrases
                    WHERE category IN (?, '__DEFAULT__')
                      AND (text LIKE '%{url}%' OR (text NOT LIKE '%http://%' AND text NOT LIKE '%https://%'))
                    ORDER BY last_used_at IS NOT NULL, last_used_at, use_count LIMIT 1""", (resource["category"],)).fetchone()
                if preview:
                    text, source = render_phrase(preview["text"],resource["category"],resource["url"]), preview["source"]
                    if resource["url"] not in text:
                        text=f"{text.rstrip()}\n{resource['url']}"
                else:
                    text, source = f"整理了一份 {resource['category']} 相关资料，有需要可以看看：\n{resource['url']}", "fallback_template"
                print(f"DRY-RUN target={candidate.url}\nsource={source}\ntext:\n{text}")
                # Do not mutate phrase usage, promotion history, candidates, or remote state.
                finish("SKIPPED", "dry-run", candidate.url)
                return 0
            text,source=phrase(db,resource["category"],resource["url"])
            digest=hashlib.sha256(text.encode()).hexdigest()
            try:
                submission=client.publish_comment(candidate.url,text)
                remote=client.verify_comment(candidate.bvid,acc["uid"],text,resource["url"])
                if remote:
                    status, verification = "published", "verified"
                elif submission == "network_confirmed":
                    status, verification = "published", "accepted_by_api"
                else:
                    status, verification = "submitted", "pending"
                candidate_id = db.connection.execute(
                    "SELECT id FROM promotion_candidates WHERE resource_id=? AND platform='bilibili' AND video_url=?",
                    (resource["id"], candidate.url),
                ).fetchone()
                db.connection.execute("INSERT INTO promotion_records(account_id,resource_id,candidate_id,platform,video_url,bvid,text,content_hash,publish_status,verification_status,remote_comment_id,detail,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (acc["id"],resource["id"],candidate_id["id"] if candidate_id else None,"bilibili",candidate.url,candidate.bvid,text,digest,status,verification,remote,f"source={source}",datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                db.connection.commit()
                if status == "published": successes+=1
                else:
                    pending+=1
            except Exception as exc:
                db.connection.execute("INSERT OR IGNORE INTO promotion_records(account_id,resource_id,platform,video_url,bvid,text,content_hash,publish_status,verification_status,detail,created_at) VALUES(?,?,?,?,?,?,?,'failed','not_run',?,?)", (acc["id"],resource["id"],"bilibili",candidate.url,candidate.bvid,text,digest,str(exc)[:1000],datetime.now().strftime("%Y-%m-%d %H:%M:%S"))); db.connection.commit()
                failures+=1; failure_reasons.append(str(exc)[:500])
    final_status = "SUCCESS" if successes else ("PENDING" if pending else ("FAILED" if failures else "SKIPPED"))
    final_reason = "" if final_status == "SUCCESS" else ("评论提交待复核" if final_status == "PENDING" else "；".join(failure_reasons) or "没有可用候选或发布失败")
    latest_url = candidate.url if 'candidate' in locals() and candidate else ""
    finish(final_status,final_reason,latest_url)
    print(f"promotion complete: success={successes} pending={pending} failed={failures} skipped={skips}"); return 0


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
            if db.connection.execute("SELECT 1 FROM videos WHERE account_id=? AND bvid=?",(acc["id"],bvid)).fetchone(): continue
            try:
                # Each nurture run watches five successful videos by default and
                # attempts likes on the first three successful watches.
                should_like = like_attempts < 3
                duration,watched,liked=client.watch_and_like(url,allow_like=should_like)
                db.connection.execute("INSERT INTO videos(account_id,bvid,url,status,duration,watched_seconds,watched_at) VALUES(?,?,?,'watched',?,?,CURRENT_TIMESTAMP)",(acc["id"],bvid,url,duration,watched));
                if liked: db.connection.execute("INSERT OR IGNORE INTO interactions(account_id,bvid,action) VALUES(?,?,'like')",(acc["id"],bvid))
                if should_like: like_attempts+=1
                db.connection.commit(); done+=1
            except Exception as exc: db.connection.execute("INSERT OR REPLACE INTO videos(account_id,bvid,url,status,last_error) VALUES(?,?,?,'failed',?)",(acc["id"],bvid,url,str(exc)[:1000])); db.connection.commit()
    notify.send("养号任务完成",f"账号：{acc['name']}\n成功观看：{done}\n点赞尝试：{like_attempts}"); print(f"nurture complete: watched={done} like_attempts={like_attempts}"); return 0


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
    sub.add_parser("promotion-candidates")
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
        if args.command=="promotion-summary": return promotion_summary(db,notify,args)
        return nurture(db,notify,args)
    finally: db.close()

if __name__=="__main__":
    try: raise SystemExit(main())
    except Exception as exc: print(f"ERROR: {exc}",file=sys.stderr); raise SystemExit(1)
