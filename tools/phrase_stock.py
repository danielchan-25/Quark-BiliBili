"""Count genuinely usable, globally unique phrases and alert on low stock."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mediaflow.config import Settings
from mediaflow.database import Database
from mediaflow.dedup import text_key,text_used
from mediaflow.notify import FeishuNotifier
from mediaflow.audit import write_audit_event


def inventory(db):
    result=[]
    for resource in db.connection.execute('SELECT * FROM promotion_resources WHERE enabled=1'):
        available=set()
        for row in db.connection.execute("SELECT text FROM phrases WHERE category IN (?, '__DEFAULT__')",(resource['category'],)):
            text=row['text']
            if '{url}' not in text and ('https://' in text or 'http://' in text):
                continue
            text=text.replace('{category}',resource['category']).replace('{url}',resource['url'])
            if resource['url'] not in text:
                text += '\n'+resource['url']
            if not text_used(db,text):
                available.add(text_key(text))
        result.append(dict(resource_id=resource['id'],category=resource['category'],available=len(available)))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--minimum',type=int,default=30)
    parser.add_argument('--notify',action='store_true')
    args=parser.parse_args()
    settings=Settings.load();db=Database(settings.database_path)
    try:
        rows=inventory(db)
        low=[r for r in rows if r['available']<args.minimum]
        print(json.dumps(dict(stock=rows,minimum=args.minimum,low_stock=bool(low)),ensure_ascii=False))
        write_audit_event(settings.log_dir/'phrase-generation.jsonl','stock_checked',stock=rows,minimum=args.minimum,low_stock=bool(low))
        if low and args.notify:
            if not settings.feishu_webhook:
                raise RuntimeError('FEISHU_WEBHOOK not configured')
            FeishuNotifier(settings.feishu_webhook,settings.feishu_secret).send('话术库存不足告警',json.dumps(low,ensure_ascii=False)+f'；目标至少{args.minimum}条，请补充新话术。')
        raise SystemExit(1 if low else 0)
    finally:
        db.close()
