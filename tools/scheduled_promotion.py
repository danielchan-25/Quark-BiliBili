"""Guarded scheduler entry point; every preflight failure is persisted and alerted."""
import argparse
import contextlib
import csv
import io
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import main as workflow
from mediaflow.audit import write_audit_event
from mediaflow.bilibili import BilibiliClient, CommentPreparationError
from mediaflow.browser_lease import BrowserBusy, BrowserLease
from mediaflow.config import Settings
from mediaflow.database import Database
from mediaflow.candidate_pool import maintain_candidate_pool
from mediaflow.notify import FeishuNotifier
from mediaflow.promotion_schedule import DEFAULT_ACCOUNT, ACCOUNT_HOURS
from mediaflow.scheduled_notifications import BEIJING, alert_scheduled_stop
from tools.import_phrases import import_phrases

ACCOUNT = DEFAULT_ACCOUNT


def run(slot, phase='prepare', phrase_file=None, resume=False, stop_reason=None, *, account=ACCOUNT, execution_id=None):
    try:
        with BrowserLease('round:' + account + ':' + slot, ROOT/'data/round-locks'):
            return _run_locked(slot, phase, phrase_file, resume, stop_reason, account=account, execution_id=execution_id)
    except BrowserBusy:
        return {'stage': 'already_running'}


def _run_locked(slot, phase='prepare', phrase_file=None, resume=False, stop_reason=None, *, account=ACCOUNT, execution_id=None):
    if account not in ACCOUNT_HOURS:
        raise ValueError('Account has no configured promotion schedule')
    expected_uid = account.removeprefix('bilibili-')
    slot = datetime.fromisoformat(slot)
    if slot.tzinfo is None:
        raise ValueError('Slot must include timezone')
    slot = slot.astimezone(BEIJING)
    if slot.hour not in ACCOUNT_HOURS[account] or slot.minute != 40 or slot.second or slot.microsecond:
        raise ValueError('Invalid promotion slot')
    path = ROOT/'data/scheduled-runs'/f'quark-{account}-{slot:%Y%m%d-%H%M}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix('.lock')
    try:
        with lock.open('x', encoding='utf-8') as handle:
            handle.write(execution_id or datetime.now(BEIJING).isoformat())
    except FileExistsError:
        return {'stage':'already_running'}
    db = None
    notifier = None
    entered = False
    baseline = task_baseline = None
    state = None
    browser_scope = contextlib.ExitStack()
    operation = 'configuration'

    def save():
        path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        write_audit_event(ROOT/'logs/scheduled-promotion.jsonl', 'round_state', **state)

    def stop(reason, error_type=None):
        state.update(stage='stopped', reason=reason, comments_submitted=0, ended_at=datetime.now(BEIJING).isoformat())
        if error_type:
            state['error_type'] = error_type
        save()
        try:
            state['new_alert_sent'] = alert_scheduled_stop(path, notifier)
        except Exception as exc:
            return {'stage':'stopped', 'reason':reason, 'alert_error':type(exc).__name__}
        # Helper has persisted the receipt status; do not overwrite it with a stale dict.
        return {'stage':'stopped', 'reason':reason, 'alert_sent':state['new_alert_sent']}

    try:
        if phase == 'prepare':
            if path.exists():
                old = json.loads(path.read_text(encoding='utf-8'))
                if not resume or old['stage'] != 'stopped' or old.get('comments_submitted') != 0 or 'record_baseline' in old:
                    return {'stage':'already_exists', 'previous_stage':old['stage']}
                state = dict(old)
                state.setdefault('history', []).append({k:old.get(k) for k in ('stage','reason','attempt_id','feishu_alert_status')})
            else:
                state = {'account':account,'slot':slot.isoformat(),'comments_submitted':0}
            state.update(stage='preflight', attempt_id=uuid4().hex, execution_id=execution_id, started_at=datetime.now(BEIJING).isoformat())
            for key in ('reason','error_type','error_stage','underlying_error_type','ended_at'):
                state.pop(key,None)
            save()
        else:
            state = json.loads(path.read_text(encoding='utf-8'))
            if state.get('account') != account or state.get('slot') != slot.isoformat():
                raise ValueError('Round account or slot does not match request')
            if state['stage'] != 'ready':
                return {'stage':'not_ready', 'previous_stage':state['stage']}
        settings = Settings.load()
        notifier = FeishuNotifier(settings.feishu_webhook, settings.feishu_secret)
        if phase == 'stop':
            return stop(stop_reason or 'candidate_preparation_failed')
        db = Database(settings.database_path)
        with (ROOT/'data/account.csv').open(encoding='utf-8-sig',newline='') as handle:
            row = next((r for r in csv.DictReader(handle) if r.get('账号') == account), None)
        if not row or row.get('角色') != '推广' or row.get('启用状态') != '启用':
            return stop('csv_account_not_enabled_for_promotion')
        acc = db.connection.execute("SELECT * FROM accounts WHERE name=? AND enabled=1 AND role='promotion' AND platform='bilibili'",(account,)).fetchone()
        if not acc:
            return stop('account_not_enabled_in_database')
        if acc['uid'] != expected_uid or (row.get('CDP地址') and acc['cdp_url'].rstrip('/') != row['CDP地址'].strip().rstrip('/')):
            return stop('registered_identity_or_browser_conflict')
        ready, reason = workflow.rate_ready(db, acc['id'])
        if not ready:
            return stop(reason)
        resources = db.connection.execute('SELECT * FROM promotion_resources WHERE enabled=1 ORDER BY id').fetchall()
        if not resources or any(r['url'] != row.get('推广链接','').strip() for r in resources):
            return stop('enabled_resources_missing_or_not_matching_account_configuration')
        operation = 'browser_connection'
        with (BilibiliClient(acc['cdp_url']) if phase == 'prepare'
              else contextlib.nullcontext(browser_scope.enter_context(BilibiliClient(acc['cdp_url'])))) as client:
            operation = 'browser_identity'
            if client.uid() != expected_uid:
                return stop('browser_not_logged_in_as_expected_uid')
            operation = 'candidate_check'
            candidate = workflow.available_visible_candidate(db,client,acc['id'],resources[0]['id'],retain_page=phase=='publish')
        pool = None
        if phase == 'prepare' or candidate is None:
            operation = 'candidate_pool'
            pool = maintain_candidate_pool(db, account,
                lambda policy, target, report: workflow.replenish_promotion_candidates(db, account, policy, target, report),
                state_dir=ROOT/'data/candidate-pool', force=candidate is None)
            state['candidate_pool'] = pool
        if pool and pool['status'] in ('refilled', 'partial', 'failed'):
            with (BilibiliClient(acc['cdp_url']) if phase == 'prepare' else contextlib.nullcontext(client)) as client:
                if client.uid() != expected_uid:
                    return stop('browser_identity_changed')
                candidate = workflow.available_visible_candidate(db,client,acc['id'],resources[0]['id'],retain_page=phase=='publish')
        if not candidate:
            return stop('candidate_pool_refill_failed' if pool and pool['status'] == 'failed'
                        else 'no_approved_visible_unpublished_candidate')
        if phase == 'prepare':
            try:
                workflow.choose_phrase(db,resources[0]['category'],resources[0]['url'])
                phrase_ready = True
            except ValueError:
                phrase_ready = False
            state.update(stage='ready', bvid=candidate.bvid, resource_id=resources[0]['id'])
            save()
            return {'stage':'ready','bvid':candidate.bvid,'title':candidate.title,
                    'category':resources[0]['category'],'phrase_ready':phrase_ready,'candidate_pool':pool}
        if candidate.bvid != state['bvid']:
            # No intent or phrase has been consumed yet. A newly usable target
            # may replace a cooling candidate in this same execution attempt.
            # A supplied, video-specific phrase must be regenerated separately.
            if phrase_file or resources[0]['id'] != state['resource_id']:
                state.update(stage='ready',bvid=candidate.bvid,resource_id=resources[0]['id'])
                save()
                return dict(stage='ready',bvid=candidate.bvid,title=candidate.title,
                            category=resources[0]['category'],phrase_ready=False,reason='candidate_changed_regenerate_phrase')
            state.setdefault('candidate_changes', []).append(dict(previous_bvid=state['bvid'], bvid=candidate.bvid))
            state['bvid'] = candidate.bvid
            save()
        if phrase_file:
            phrases = json.loads(Path(phrase_file).read_text(encoding='utf-8-sig'))
            import_phrases(db, phrases)
            db.connection.commit()
        # Validate selection before entering the production function.
        try:
            workflow.choose_phrase(db,resources[0]['category'],resources[0]['url'])
        except ValueError:
            state.update(stage='ready')
            save()
            return dict(stage='ready',bvid=candidate.bvid,title=candidate.title,
                        category=resources[0]['category'],phrase_ready=False,reason='phrase_replenishment_required')
        baseline = db.connection.execute('SELECT COALESCE(MAX(id),0) FROM promotion_records').fetchone()[0]
        task_baseline = db.connection.execute('SELECT COALESCE(MAX(id),0) FROM task_runs').fetchone()[0]
        state.update(stage='publishing',record_baseline=baseline,task_run_baseline=task_baseline)
        save()
        entered = True
        try:
            with workflow.prepared_publication(db,client,acc['id'],resources[0]['id'],candidate), contextlib.redirect_stdout(io.StringIO()):
                code = workflow.promotion(db,notifier,argparse.Namespace(account=account,max_comments=1,dry_run=False,slot=None))
        finally:
            rows = db.connection.execute('SELECT id,bvid,publish_status,verification_status,remote_comment_id FROM promotion_records WHERE account_id=? AND id>?',(acc['id'],baseline)).fetchall()
            tasks = db.connection.execute('SELECT id,status FROM task_runs WHERE account_id=? AND id>?',(acc['id'],task_baseline)).fetchall()
            state.update(results=[dict(r) for r in rows],comments_submitted=len(rows),task_runs=[dict(r) for r in tasks])
            receipt_paths = [ROOT/'data/notification-receipts'/f"promotion-{t['id']}.json" for t in tasks]
            receipt_paths += [ROOT/'data/notification-receipts'/f"verification-{r['id']}.json" for r in rows]
            state['notification_receipts'] = [json.loads(p.read_text(encoding='utf-8')) for p in receipt_paths if p.exists()]
        state.update(stage='completed' if code == 0 else ('requires_readonly_recheck' if any(r['publish_status']=='submitted' for r in rows) else 'failed'),exit_code=code,ended_at=datetime.now(BEIJING).isoformat())
        save()
        return {k:state[k] for k in ('stage','comments_submitted','results','task_runs','notification_receipts')}
    except BaseException as exc:
        if state is None:
            raise
        if entered and state.get('task_runs'):
            state.update(stage='requires_readonly_recheck',error_type=type(exc).__name__)
            save()
            return {'stage':state['stage'],'error_type':type(exc).__name__,'comments_submitted':state.get('comments_submitted',0)}
        # Includes database, CSV, browser, screening and phrase preparation failures.
        if notifier is None:
            import os
            notifier = FeishuNotifier(os.getenv('FEISHU_WEBHOOK'),os.getenv('FEISHU_SECRET'))
        if isinstance(exc, CommentPreparationError):
            state.update(error_stage=exc.stage,underlying_error_type=exc.error_type,preparation_attempts=exc.attempts)
            reason = {'navigation':'video_navigation_failed','dom_probe':'browser_dom_access_failed','comment_section':'comment_section_load_failed',
                      'comment_permission':'comment_editor_readiness_failed','editor_resolve':'comment_editor_resolve_failed',
                      'editor_click':'comment_editor_focus_failed','editor_focus':'comment_editor_focus_failed'}.get(exc.stage,'comment_editor_readiness_failed')
        else:
            reason = ('browser_busy' if isinstance(exc, BrowserBusy) else 'local_cdp_unavailable' if '本地 CDP 服务不可用' in str(exc)
                      else 'browser_connection_failed' if operation == 'browser_connection'
                      else 'browser_identity_check_failed' if operation == 'browser_identity'
                      else 'candidate_pool_refill_failed' if operation == 'candidate_pool'
                      else 'candidate_preparation_failed')
        if isinstance(exc, CommentPreparationError) and exc.reason == 'comment_section_repeated_timeout':
            reason = exc.reason
            state.update(diagnosis_required=True,total_attempts=9,timeout_videos=exc.timeout_videos)
        state['operation'] = operation
        return stop(reason, type(exc).__name__)
    finally:
        try:
            browser_scope.close()
        except Exception as cleanup_error:
            write_audit_event(ROOT/'logs/scheduled-promotion.jsonl','browser_cleanup_failed',error_type=type(cleanup_error).__name__)
        if db:
            db.close()
        lock.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account',choices=tuple(ACCOUNT_HOURS),default=ACCOUNT)
    parser.add_argument('--slot',required=True,help='Beijing scheduled slot with +08:00 timezone')
    parser.add_argument('--phase',choices=('prepare','publish','stop'),default='prepare')
    parser.add_argument('--phrase-file')
    parser.add_argument('--resume',action='store_true',help='Only after explicit human authorization')
    parser.add_argument('--stop-reason')
    args=parser.parse_args()
    if args.phase == 'prepare':
        from tools.supervised_prepare import supervise_prepare
        result = supervise_prepare(args.slot, args.account, args.resume)
    else:
        result=run(args.slot,args.phase,args.phrase_file,args.resume,args.stop_reason,account=args.account)
    print(json.dumps(result,ensure_ascii=False))
    raise SystemExit(1 if result['stage'] in ('failed','stopped','requires_readonly_recheck') else 0)
