"""Dedicated runner: discover, vector first, raster comparison/fallback, proposals."""
import json
import logging
import os
import re
import time
import signal
import threading
from pathlib import Path
import pymupdf
from PIL import Image
from . import config,store
from .contracts import Proposal,canonical,write
from .artifacts import persist
from .extractors import native,raster,vector

log=logging.getLogger(__name__)
NEGATIVE=re.compile(r'\b(equipotential|contour|schematic|photograph|cross.section|artistic|layout|acceptance map|block diagram|perspective view|isometric|cut.away|circuit|mechanical drawing)\b',re.I)
POSITIVE=re.compile(r'resolut|efficien|distribut|versus|depend|spectrum|spectra|function|performance|momentum|material budget|energy|response|occupancy|noise|fraction|rate|signal|linearity|measurement points|time history|current',re.I)

def set_scope(pid,scope,note=''):
    with store.db() as c:c.execute('UPDATE plots SET scope=?,scope_note=? WHERE id=?',(scope,note,pid))
def source_plot(pid):
    with store.db() as c:
        p=store.unpack(c.execute('SELECT * FROM plots WHERE id=?',(pid,)).fetchone())
        s=store.unpack(c.execute('SELECT * FROM sources WHERE id=?',(p['source_id'],)).fetchone())
    folder=config.DATA/'plots'/pid;folder.mkdir(exist_ok=True)
    image=folder/'original.png';pdf=config.DATA/'sources'/s['id']/'source.pdf'
    if not image.exists():
        with pymupdf.open(pdf) as doc:
            page=doc[p['page']-1];region=vector.figure_rect(p['bbox'],page)
            page.get_pixmap(matrix=pymupdf.Matrix(2.5,2.5),clip=region).save(image)
    return p,s,pdf,image

def discover(job):
    sid=job['source_id'];pdf=config.DATA/'sources'/sid/'source.pdf'
    total=0
    with pymupdf.open(pdf) as doc:
        if len(doc)>1000:raise ValueError('Prototype discovery supports at most 1000 pages')
        for number,page in enumerate(doc,1):
            paths=list(enumerate(page.get_drawings()));words=page.get_text('words')
            for _,frame in vector._frames(paths,page.rect):
                try:
                    if len(vector._ticks(paths,frame,'x'))<3 or len(vector._ticks(paths,frame,'y'))<3:continue
                except Exception:continue
                box=[max(0,frame.x0-55),max(0,frame.y0-12),min(page.rect.width,frame.x1+20),min(page.rect.height,frame.y1+45)]
                pid=store.uid()
                with store.db() as c:c.execute('INSERT INTO plots(id,source_id,page,caption,bbox,created_at) VALUES(?,?,?,?,?,?)',(pid,sid,number,f'XY panel on PDF page {number}',canonical(box),store.now()))
                store.queue('auto',pid,sid);total+=1
            if number%20==0:
                with store.db() as c:c.execute('UPDATE jobs SET heartbeat=? WHERE id=?',(time.time(),job['id']))
    if not total:raise ValueError('No native framed XY panels found. Supply figure regions from Docling/Metis or a calibrated manual proposal for scanned pages.')

def execute(job):
    if job['kind']=='discover':return discover(job)
    p,s,pdf,image=source_plot(job['plot_id']);title=p['caption'];width,height=Image.open(image).size
    payload=job['payload'];parent=payload.get('parent_attempt_id')
    if job['kind'] in {'manual','materialize-agent'}:
        proposal=Proposal.model_validate(payload['proposal']);proposal.check_image(width,height)
        set_scope(p['id'],'1d','Reviewer/agent supplied an explicit calibrated XY candidate')
        aid=persist(p,s,'agent' if job['kind']=='materialize-agent' else 'manual',proposal,evidence={'request':payload.get('request_id'),'note':payload.get('note',''),'harness':payload.get('harness')},parent=parent,job_id=job['id'])
        return aid
    if NEGATIVE.search(title) and not payload.get('force_scope'):
        set_scope(p['id'],'out_of_scope','Caption indicates a drawing, contour/field map or other unsupported figure');return
    guide=None
    if job['kind']=='auto':
        try:guide,evidence=native.extract(pdf,p['page'],p['bbox'],width,height,title)
        except Exception as e:guide,evidence=None,{'accepted':False,'reason_code':'native-error','message':str(e)}
        persist(p,s,'vector',guide,evidence,error=evidence.get('message'),job_id=job['id'])
        if not guide and evidence.get('reason_code') in {'multiple-panels','additional-y-axis'}:
            set_scope(p['id'],'needs_selection','Select one panel/axis explicitly before extraction');return
        if not guide and not POSITIVE.search(title) and not payload.get('force_scope'):
            set_scope(p['id'],'unclassified','No supported native XY panel and no 1-D caption evidence');return
    else:
        with store.db() as c:row=c.execute("SELECT id FROM attempts WHERE plot_id=? AND method='vector' AND status IN ('candidate','approved') ORDER BY created_at DESC LIMIT 1",(p['id'],)).fetchone()
        if row:
            path=config.DATA/'attempts'/row['id']/'proposal.json'
            if path.exists():guide=Proposal.model_validate(json.loads(path.read_text()))
    set_scope(p['id'],'1d','Supported 1-D XY candidate; confirm scope during review')
    try:
        proposal,evidence=raster.extract(image,title,guide)
        persist(p,s,'raster',proposal,evidence,parent=parent,job_id=job['id'])
    except Exception as e:
        persist(p,s,'raster',error=str(e),evidence={'version':raster.VERSION,'reason':str(e)},parent=parent,job_id=job['id'])
        if guide is None:set_scope(p['id'],'unclassified','Neither automated method established a calibrated 1-D candidate; inspect scope and axis references')

def run(once=False):
    store.initialize();worker=os.environ.get('LYNCEUS_RUNNER_ID','lynceus-runner')
    stop=threading.Event()
    if threading.current_thread() is threading.main_thread():signal.signal(signal.SIGTERM,lambda *_:stop.set())
    while not stop.is_set():
        job=store.claim(worker)
        if not job:
            if once:return
            time.sleep(.5);continue
        try:
            execute(job);store.finish(job['id']);log.info('Completed %s %s',job['kind'],job['id'])
        except Exception as e:log.exception('Extraction failed');store.finish(job['id'],e)
