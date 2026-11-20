"""Local Lynceus review/control plane. Extraction work is queued to the runner."""
import hashlib
import json
import secrets
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from argon2 import PasswordHasher
from fastapi import FastAPI,Request,Response,HTTPException,UploadFile,File,Form
from fastapi.responses import FileResponse,JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field
from . import config,store,review
from .contracts import Proposal,canonical,digest,file_sha,write
from .policy import InputRecord,Protection,public_protection,can_access

@asynccontextmanager
async def lifespan(app):store.initialize();yield
app=FastAPI(title='Lynceus extraction workbench',lifespan=lifespan)
@app.exception_handler(ValueError)
async def bad(request,e):return JSONResponse({'detail':str(e)},status_code=400)
@app.exception_handler(KeyError)
async def missing(request,e):return JSONResponse({'detail':str(e)},status_code=404)
@app.exception_handler(PermissionError)
async def denied(request,e):return JSONResponse({'detail':str(e)},status_code=403)
@app.middleware('http')
async def perimeter(request,call_next):
    if request.method not in {'GET','HEAD','OPTIONS'} and request.headers.get('origin') and urlsplit(request.headers['origin']).netloc!=request.headers.get('host'):return JSONResponse({'detail':'Cross-origin writes are not allowed'},status_code=403)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff';response.headers['Referrer-Policy']='same-origin'
    response.headers['Content-Security-Policy']="default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; font-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'self'"
    if request.url.path.startswith('/api/'):response.headers['Cache-Control']='no-store'
    return response
def actor(request,mutate=False,optional=False):
    token=hashlib.sha256(request.cookies.get('lynceus_session','').encode()).hexdigest()
    with store.db() as c:r=c.execute('SELECT u.*,s.csrf FROM users u JOIN sessions s ON u.id=s.user_id WHERE s.token=? AND s.expires>?',(token,time.time())).fetchone()
    if not r:
        if optional:return None
        raise HTTPException(401,'Sign in to Lynceus')
    user=store.user(r)
    if mutate and not secrets.compare_digest(user['csrf'],request.headers.get('x-csrf-token','')):raise PermissionError('Invalid CSRF token')
    return user
def bridge(request):
    supplied=request.headers.get('authorization','').removeprefix('Bearer ')
    if not secrets.compare_digest(supplied,(config.DATA/'.agent-token').read_text().strip()):raise PermissionError('Agent runner authentication required')
class Login(BaseModel):username:str=Field(max_length=100);password:str=Field(max_length=1000)
@app.get('/api/session')
def session(request:Request):return {'user':actor(request,optional=True),'schema':'lynceus.extraction/v1'}
@app.post('/api/login')
def login(body:Login,response:Response):
    with store.db() as c:
        row=c.execute('SELECT * FROM users WHERE username=?',(body.username,)).fetchone()
        try:
            if not row or not PasswordHasher().verify(row['password'],body.password):raise ValueError()
        except Exception:raise HTTPException(401,'Invalid username or password')
        token=secrets.token_urlsafe(40);csrf=secrets.token_urlsafe(24)
        c.execute('INSERT INTO sessions VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),row['id'],csrf,time.time()+8*3600))
    response.set_cookie('lynceus_session',token,httponly=True,samesite='strict',max_age=8*3600)
    return {'user':store.user(row)|{'csrf':csrf}}
@app.post('/api/logout')
def logout(request:Request,response:Response):
    actor(request,True)
    with store.db() as c:c.execute('DELETE FROM sessions WHERE token=?',(hashlib.sha256(request.cookies.get('lynceus_session','').encode()).hexdigest(),))
    response.delete_cookie('lynceus_session');return {'ok':True}
@app.get('/api/catalog')
def catalog(request:Request,source_id:str|None=None,scope:str='all',method:str|None=None,q:str='',offset:int=0):
    user=actor(request)
    with store.db() as c:
        sources=[store.unpack(r) for r in c.execute('SELECT * FROM sources ORDER BY title')]
        allowed={s['id']:s for s in sources if can_access(s['record']['protection'],user)}
        rows=[store.unpack(r) for r in c.execute('SELECT p.*,(SELECT count(*) FROM attempts a WHERE a.plot_id=p.id AND a.status=\'candidate\') candidate_count,(SELECT count(*) FROM jobs j WHERE j.plot_id=p.id AND j.state IN (\'queued\',\'running\')) pending FROM plots p ORDER BY p.source_id,p.page,p.caption')]
        rows=[r for r in rows if r['source_id'] in allowed and (not source_id or r['source_id']==source_id) and (scope=='all' or r['scope']==scope) and (not q or q.casefold() in (r['caption']+' '+str(r['page'])).casefold())]
        if method:
            ids={r[0] for r in c.execute('SELECT DISTINCT plot_id FROM attempts WHERE method=? AND status!=\'discarded\'',(method,))};rows=[r for r in rows if r['id'] in ids]
        placeholders=','.join('?' for _ in allowed) or 'NULL'
        jobs=[dict(r) for r in c.execute(f"SELECT j.kind,j.state,count(*) count FROM jobs j WHERE source_id IN ({placeholders}) GROUP BY j.kind,j.state",tuple(allowed))]
        counts=[dict(r) for r in c.execute(f'SELECT a.method,a.status,count(*) count FROM attempts a JOIN plots p ON p.id=a.plot_id WHERE p.source_id IN ({placeholders}) GROUP BY a.method,a.status',tuple(allowed))]
    return {'sources':list(allowed.values()),'plots':rows[max(0,offset):max(0,offset)+100],'total':len(rows),'jobs':jobs,'attempt_counts':counts}
@app.get('/api/plots/{pid}')
def detail(pid:str,request:Request):
    p=store.plot(pid,actor(request))
    with store.db() as c:
        p['attempts']=[store.unpack(r) for r in c.execute('SELECT * FROM attempts WHERE plot_id=? ORDER BY created_at',(pid,))]
        p['jobs']=[store.unpack(r) for r in c.execute('SELECT * FROM jobs WHERE plot_id=? ORDER BY created_at',(pid,))]
        p['messages']=[dict(r) for r in c.execute('SELECT * FROM messages WHERE plot_id=? ORDER BY created_at',(pid,))]
        p['decisions']=[dict(r) for r in c.execute('SELECT * FROM decisions WHERE plot_id=? ORDER BY created_at',(pid,))]
    return p
@app.get('/api/sources/{sid}/pdf')
def pdf(sid:str,request:Request):
    store.source(sid,actor(request));return FileResponse(config.DATA/'sources'/sid/'source.pdf',media_type='application/pdf')
@app.get('/api/plots/{pid}/image')
def original(pid:str,request:Request):
    store.plot(pid,actor(request));p=config.DATA/'plots'/pid/'original.png'
    if not p.is_file():raise KeyError('Crop is still queued for rendering')
    return FileResponse(p,media_type='image/png')
@app.get('/api/attempts/{aid}/artifacts/{name}')
def artifact(aid:str,name:str,request:Request):
    a=store.attempt(aid,actor(request))
    if a['status']=='discarded' or Path(name).name!=name or name not in a['manifest'].get('artifacts',{}):raise KeyError('Extraction artifact not available')
    path=config.DATA/'attempts'/aid/name
    if file_sha(path)!=a['manifest']['artifacts'][name]:raise HTTPException(409,'Artifact checksum mismatch')
    return FileResponse(path,filename=name,content_disposition_type='inline')
def parent_check(pid,aid,user):
    if aid and store.attempt(aid,user)['plot_id']!=pid:raise ValueError('Parent extraction belongs to a different plot')
class Run(BaseModel):method:str='raster';parent_attempt_id:str|None=None;force_scope:bool=False
class Region(BaseModel):page:int=Field(ge=1);bbox:tuple[float,float,float,float];caption:str=Field(min_length=1,max_length=1000)
@app.post('/api/sources/{sid}/regions',status_code=201)
def region(sid:str,body:Region,request:Request):
    store.source(sid,actor(request,True));l,t,r,b=body.bbox
    if not (0<=l<r and 0<=t<b):raise ValueError('Use a top-left PDF rectangle')
    pid=store.uid()
    with store.db() as c:c.execute('INSERT INTO plots(id,source_id,page,caption,bbox,created_at) VALUES(?,?,?,?,?,?)',(pid,sid,body.page,body.caption,canonical(body.bbox),store.now()))
    return {'plot_id':pid,'job_id':store.queue('auto',pid,sid,{'force_scope':True})}
@app.post('/api/plots/{pid}/run',status_code=201)
def run(pid:str,body:Run,request:Request):
    user=actor(request,True);p=store.plot(pid,user);parent_check(pid,body.parent_attempt_id,user)
    if body.method not in {'auto','raster'}:raise ValueError('Choose auto or raster')
    return {'job_id':store.queue(body.method,pid,p['source_id'],body.model_dump())}
class Manual(BaseModel):proposal:Proposal;parent_attempt_id:str|None=None
@app.post('/api/plots/{pid}/manual',status_code=201)
def manual(pid:str,body:Manual,request:Request):
    user=actor(request,True);p=store.plot(pid,user);parent_check(pid,body.parent_attempt_id,user)
    return {'job_id':store.queue('manual',pid,p['source_id'],body.model_dump())}
class Decision(BaseModel):snapshot_sha256:str;decision:str;reason:str=Field(min_length=1,max_length=4000);checks:list[str]=Field(default_factory=list)
@app.post('/api/attempts/{aid}/decision')
def decision(aid:str,body:Decision,request:Request):return review.decide(aid,actor(request,True),body.snapshot_sha256,body.decision,body.reason,body.checks)
class AgentRequest(BaseModel):message:str=Field(min_length=1,max_length=6000);parent_attempt_id:str|None=None
@app.post('/api/plots/{pid}/agent',status_code=201)
def agent_request(pid:str,body:AgentRequest,request:Request):
    user=actor(request,True);p=store.plot(pid,user);parent_check(pid,body.parent_attempt_id,user)
    if p['source']['record']['protection']['classification']!='public':raise PermissionError('The prototype agent connection accepts public sources only')
    jid=store.queue('agent',pid,p['source_id'],body.model_dump())
    with store.db() as c:c.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)',(store.uid(),pid,'user',body.message,jid,store.now()))
    return {'job_id':jid}
@app.get('/api/agent/claim')
def agent_claim(request:Request):
    bridge(request);job=store.claim('codex-harness',agent=True)
    if not job:return {'job':None}
    with store.db() as c:
        p=store.unpack(c.execute('SELECT * FROM plots WHERE id=?',(job['plot_id'],)).fetchone());s=store.unpack(c.execute('SELECT * FROM sources WHERE id=?',(p['source_id'],)).fetchone())
        requested=job['payload'].get('parent_attempt_id')
        latest=c.execute("SELECT id FROM attempts WHERE plot_id=? AND status IN ('candidate','approved') ORDER BY (id=?) DESC,created_at DESC LIMIT 1",(p['id'],requested)).fetchone()
    if s['record']['protection']['classification']!='public':store.finish(job['id'],'Agent scope denied');raise PermissionError('Public source required')
    context={'caption':p['caption'],'page':p['page'],'request':job['payload']['message'],'proposal':None}
    with store.db() as c:context['conversation']=[dict(r) for r in reversed(c.execute('SELECT role,body FROM messages WHERE plot_id=? ORDER BY created_at DESC LIMIT 6',(p['id'],)).fetchall())]
    if latest:
        path=config.DATA/'attempts'/latest['id']/'proposal.json'
        if path.exists():context['proposal']=json.loads(path.read_text())
    return {'job':job,'context':context}
@app.get('/api/agent/jobs/{jid}/image')
def agent_image(jid:str,request:Request):
    bridge(request)
    with store.db() as c:j=store.unpack(c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
    if not j or j['kind']!='agent' or j['state']!='running':raise KeyError('Agent job not found')
    return FileResponse(config.DATA/'plots'/j['plot_id']/'original.png',media_type='image/png')
class AgentResult(BaseModel):reply:str=Field(max_length=16000);proposal:Proposal|None=None;error:str|None=None;harness:dict[str,str]|None=None
@app.post('/api/agent/jobs/{jid}/complete')
def agent_complete(jid:str,body:AgentResult,request:Request):
    bridge(request)
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        j=store.unpack(c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
        if not j or j['kind']!='agent' or j['state']!='running':raise ValueError('Agent job is stale')
        c.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)',(store.uid(),j['plot_id'],'assistant',body.reply or body.error or 'No candidate supplied',jid,store.now()))
        if body.proposal:c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,NULL,NULL,?,NULL)',(store.uid(),j['plot_id'],j['source_id'],'materialize-agent',canonical({'proposal':body.proposal.model_dump(),'parent_attempt_id':j['payload'].get('parent_attempt_id'),'request_id':jid,'harness':body.harness}),'queued',store.now()))
        c.execute('UPDATE jobs SET state=?,error=?,heartbeat=? WHERE id=?',('failed' if body.error else 'done',body.error,time.time(),jid))
    return {'ok':True}
@app.get('/api/plots/{pid}/export')
def export(pid:str,request:Request):
    p=store.plot(pid,actor(request))
    if not p['selected_attempt']:raise ValueError('Choose and sign off an extraction before exporting')
    a=store.attempt(p['selected_attempt'],actor(request));review.verify(a)
    if a['status']!='approved':raise ValueError('The selected extraction is no longer approved')
    folder=config.DATA/'attempts'/a['id'];target=config.DATA/'exports'/(store.uid()+'.zip')
    with store.db() as c:decision=dict(c.execute("SELECT * FROM decisions WHERE attempt_id=? AND decision='approve' ORDER BY created_at DESC LIMIT 1",(a['id'],)).fetchone())
    decision['schema']='rhadamanthys.decision/v1';decision['checks']=json.loads(decision['checks'])
    envelope={'schema':'lynceus.metis-bundle/v1','plot_id':pid,'source':p['source'],'figure':{k:p[k] for k in ['id','page','caption','bbox']},'attempt':a,'decision':decision,'requires_document_signoff':True}
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('bundle.json',canonical(envelope));z.writestr('manifest.json',canonical(a['manifest']))
        for name in a['manifest']['artifacts']:z.write(folder/name,name)
        z.write(config.DATA/'plots'/pid/'original.png','original.png')
    from starlette.background import BackgroundTask
    return FileResponse(target,filename='lynceus-'+a['id']+'.zip',media_type='application/zip',background=BackgroundTask(target.unlink,missing_ok=True))
@app.post('/api/uploads',status_code=201)
async def upload(request:Request,file:UploadFile=File(...),title:str=Form(...),protection:str=Form(...)):
    user=actor(request,True);policy=Protection.model_validate(json.loads(protection)).model_dump()
    if not can_access(policy,user):raise PermissionError('Source audience does not include this user')
    sid=store.uid();folder=config.DATA/'sources'/sid;folder.mkdir();path=folder/'source.pdf';size=0
    try:
        with path.open('wb') as output:
            while chunk:=await file.read(1024*1024):
                size+=len(chunk)
                if size>250*1024**2:raise ValueError('PDF exceeds 250 MiB')
                output.write(chunk)
        if b'%PDF-' not in path.read_bytes()[:1024]:raise ValueError('Input is not a PDF')
        record=InputRecord.model_validate({'title':title,'source':{'uri':path.as_uri()},'description':'Uploaded file: '+(file.filename or 'source.pdf'),'protection':policy}).model_dump()
        with store.db() as c:c.execute('INSERT INTO sources VALUES(?,?,?,?,?)',(sid,title,canonical(record),file_sha(path),store.now()))
        return {'source_id':sid,'job_id':store.queue('discover',sid=sid)}
    except BaseException:
        import shutil;shutil.rmtree(folder,ignore_errors=True);raise
app.mount('/',StaticFiles(directory=config.ROOT/'web',html=True),name='web')
