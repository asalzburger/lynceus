import argparse
import json
import logging
import shutil
from pathlib import Path
from . import config
from .contracts import canonical,file_sha

def seed():
    from . import store
    data=json.loads((config.DATA/'bootstrap.json').read_text())
    for document in data:
        sid=document['id']
        with store.db() as c:c.execute('INSERT OR IGNORE INTO sources VALUES(?,?,?,?,?)',(sid,document['record']['title'],canonical(document['record']),document['pdf_sha256'],store.now()))
        for p in document['plots']:
            with store.db() as c:
                exists=c.execute('SELECT 1 FROM plots WHERE id=?',(p['id'],)).fetchone()
                if exists:continue
                c.execute('INSERT INTO plots(id,source_id,page,caption,bbox,created_at) VALUES(?,?,?,?,?,?)',(p['id'],sid,p['page'],p['caption'],canonical(p['bbox']),store.now()))
            store.queue('auto',p['id'],sid)
        print(document['record']['title'],len(document['plots']),'figure regions queued')

def main():
    parser=argparse.ArgumentParser(prog='lynceus')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('init');sub.add_parser('seed')
    a=sub.add_parser('ingest');a.add_argument('pdf',type=Path);a.add_argument('--title');a.add_argument('--source-uri')
    protection=a.add_mutually_exclusive_group(required=True);protection.add_argument('--public',action='store_true');protection.add_argument('--protection-file',type=Path)
    a.add_argument('--regions',type=Path,help='JSON array of page, bbox (top-left PDF points), caption')
    a=sub.add_parser('serve');a.add_argument('--host',default='127.0.0.1');a.add_argument('--port',type=int,default=3013)
    a=sub.add_parser('worker');a.add_argument('--once',action='store_true')
    a=sub.add_parser('agent-worker');a.add_argument('--once',action='store_true');a.add_argument('--api',default='http://127.0.0.1:3013');a.add_argument('--model')
    args=parser.parse_args();logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    if args.command=='agent-worker':
        from .agent import run
        run(args.api,args.once,args.model);return
    from . import store
    store.initialize()
    if args.command=='seed':seed()
    elif args.command=='ingest':
        from .policy import InputRecord,public_protection
        from .api import Region
        pdf=args.pdf.resolve()
        if pdf.stat().st_size>250*1024**2:raise ValueError('PDF exceeds 250 MiB')
        with pdf.open('rb') as stream:
            if b'%PDF-' not in stream.read(1024):raise ValueError('Input is not a PDF')
        regions=[Region.model_validate(item) for item in json.loads(args.regions.read_text())] if args.regions else None
        policy=public_protection() if args.public else json.loads(args.protection_file.read_text())
        record=InputRecord.model_validate({'title':args.title or pdf.stem,'source':{'uri':args.source_uri or pdf.as_uri()},'protection':policy}).model_dump()
        sid=store.uid();folder=config.DATA/'sources'/sid;folder.mkdir();shutil.copyfile(pdf,folder/'source.pdf')
        with store.db() as c:c.execute('INSERT INTO sources VALUES(?,?,?,?,?)',(sid,record['title'],canonical(record),file_sha(folder/'source.pdf'),store.now()))
        if regions is None:store.queue('discover',sid=sid)
        else:
            for region in regions:
                pid=store.uid()
                with store.db() as c:c.execute('INSERT INTO plots(id,source_id,page,caption,bbox,created_at) VALUES(?,?,?,?,?,?)',(pid,sid,region.page,region.caption,canonical(region.bbox),store.now()))
                store.queue('auto',pid,sid)
        print(canonical({'source_id':sid,'queued_regions':len(regions) if regions is not None else 'native discovery'}))
    elif args.command=='serve':
        import uvicorn
        uvicorn.run('lynceus.api:app',host=args.host,port=args.port)
    elif args.command=='worker':
        from .worker import run
        run(args.once)
if __name__=='__main__':main()
