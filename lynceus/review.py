"""Rhadamanthys decision contract: approve an exact attempt, or discard it."""
import json
from . import config,store
from .contracts import canonical,digest,file_sha,write

CHECKS={'scope','axes','series','uncertainties'}
def verify(attempt):
    folder=config.DATA/'attempts'/attempt['id']
    manifest=json.loads((folder/'manifest.json').read_text())
    if digest(manifest)!=attempt['snapshot_sha256']:raise ValueError('Extraction snapshot changed')
    crop=config.DATA/'plots'/attempt['plot_id']/'original.png'
    if file_sha(crop)!=manifest['source_crop_sha256']:raise ValueError('Original source crop changed')
    with store.db() as c:sid=c.execute('SELECT source_id FROM plots WHERE id=?',(attempt['plot_id'],)).fetchone()[0]
    if file_sha(config.DATA/'sources'/sid/'source.pdf')!=manifest['source_pdf_sha256']:raise ValueError('Original source PDF changed')
    for name,value in manifest['artifacts'].items():
        if '/' in name or not (folder/name).is_file() or file_sha(folder/name)!=value:raise ValueError('Extraction artifact changed: '+name)
    return manifest
def decide(aid,actor,snapshot,decision,reason,checks):
    if actor['role']!='reviewer':raise PermissionError('Reviewer role required')
    if decision not in {'approve','discard'} or not reason.strip():raise ValueError('A decision and review note are required')
    a=store.attempt(aid,actor)
    if a['status']=='discarded' or snapshot!=a['snapshot_sha256']:raise ValueError('This extraction review is stale')
    verify(a)
    if decision=='approve':
        if store.plot(a['plot_id'],actor)['scope']!='1d':raise ValueError('Resolve 1-D scope/panel selection before sign-off')
        if a['status'] not in {'candidate','approved'} or a['diagnostics'].get('blocking'):raise ValueError('Only a valid 1-D numerical candidate can be signed off')
        if not CHECKS<=set(checks):raise ValueError('Check scope, axes, series and uncertainties before sign-off')
    try:
      with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        current=store.unpack(c.execute('SELECT * FROM attempts WHERE id=?',(aid,)).fetchone())
        if current['status']=='discarded' or current['snapshot_sha256']!=snapshot:raise ValueError('Another reviewer changed this extraction')
        selected=c.execute('SELECT selected_attempt FROM plots WHERE id=?',(a['plot_id'],)).fetchone()[0]
        if decision=='approve' and current['status']=='approved' and selected==aid:raise ValueError('This extraction is already selected and signed off')
        event={'schema':'rhadamanthys.decision/v1','id':store.uid(),'plot_id':a['plot_id'],'attempt_id':aid,'snapshot_sha256':snapshot,'decision':decision,'reviewer':actor['username'],'reason':reason.strip(),'checks':sorted(set(checks)),'created_at':store.now()}
        if decision=='discard':
            c.execute("UPDATE attempts SET status='discarded',diagnostics=? WHERE id=?",(canonical({'discard':event,'warnings':[]}),aid))
            c.execute('UPDATE plots SET selected_attempt=NULL WHERE id=? AND selected_attempt=?',(a['plot_id'],aid))
            c.execute("UPDATE jobs SET payload=json_remove(payload,'$.proposal') WHERE id=? AND kind IN ('manual','materialize-agent')",(a['diagnostics'].get('job_id'),))
        else:
            c.execute("UPDATE attempts SET status='approved' WHERE id=?",(aid,))
            c.execute('UPDATE plots SET selected_attempt=? WHERE id=?',(aid,a['plot_id']))
        c.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?)',(event['id'],a['plot_id'],aid,snapshot,decision,actor['username'],reason.strip(),canonical(event['checks']),event['created_at']))
    except BaseException:
        raise
    if decision=='discard':
        old=config.DATA/'attempts'/aid;archive=config.DATA/'.history'/aid
        archive.mkdir(exist_ok=True)
        # Keep hashes and the decision as audit evidence, never the discarded samples.
        write(archive/'manifest.json',a['manifest']);write(archive/'decision.json',event)
        for p in old.iterdir():
            if p.is_file():p.unlink()
        write(old/'discard.json',event)
    return event
