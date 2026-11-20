import json
import zipfile
import io
import pytest
import pymupdf
from fastapi.testclient import TestClient
from lynceus import config,store,review,worker
from lynceus.api import app
from lynceus.contracts import Proposal,canonical,digest,file_sha,write
from lynceus.extractors import native,raster
from lynceus.policy import public_protection

def make_pdf(path):
    doc=pymupdf.open();p=doc.new_page(width=500,height=450);frame=pymupdf.Rect(85,45,435,325);p.draw_rect(frame,color=(0,0,0))
    x=lambda v:85+v/4*350;y=lambda v:325-v/1.4*280
    for value in range(5):
        px=x(value);p.draw_line((px,325),(px,319));text=str(value);p.insert_text((px-pymupdf.get_text_length(text,fontsize=10)/2,340),text,fontsize=10)
    for i in range(8):
        value=i*.2;py=y(value);p.draw_line((85,py),(91,py));text=f'{value:g}';p.insert_text((78-pymupdf.get_text_length(text,fontsize=10),py+3),text,fontsize=10)
    p.insert_text((230,360),'Energy (GeV)',fontsize=10)
    for vx,vy in [(1,.8),(2,.5),(3,.3)]:p.draw_circle((x(vx),y(vy)),2,fill=(0,0,0))
    p.draw_polyline([(x(1),y(.9)),(x(2),y(.6)),(x(3),y(.35))],color=(1,0,0))
    doc.save(path);doc.close()

@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.delenv('LYNCEUS_DB',raising=False)
    monkeypatch.setattr(config,'DATA',tmp_path/'data');store.initialize()
    sid=store.uid();pid=store.uid();folder=config.DATA/'sources'/sid;folder.mkdir();make_pdf(folder/'source.pdf')
    with store.db() as c:
        c.execute('INSERT INTO sources VALUES(?,?,?,?,?)',(sid,'Test reference',canonical({'title':'Test reference','source':{'uri':'file:///test.pdf'},'protection':public_protection()}),file_sha(folder/'source.pdf'),store.now()))
        c.execute("INSERT INTO plots(id,source_id,page,caption,bbox,created_at) VALUES(?,?,?,?,?,?)",(pid,sid,1,'Energy resolution',canonical([0,0,500,450]),store.now()))
        owner=store.user(c.execute("SELECT * FROM users WHERE username='demo_owner'").fetchone());builder=store.user(c.execute("SELECT * FROM users WHERE username='demo_dev'").fetchone())
    job={'id':None,'kind':'auto','plot_id':pid,'source_id':sid,'payload':{}}
    worker.execute(job)
    with store.db() as c:attempts=[store.unpack(a) for a in c.execute('SELECT * FROM attempts WHERE plot_id=? ORDER BY created_at',(pid,))]
    return pid,sid,owner,builder,attempts

def auth(client,username='demo_owner'):
    r=client.post('/api/login',json={'username':username,'password':'daedalus'});assert r.status_code==200
    return {'x-csrf-token':r.json()['user']['csrf']}

def test_cli_ingestion_queues_explicit_regions_and_inherits_policy(setup,tmp_path,monkeypatch):
    pid,sid,owner,builder,attempts=setup
    from lynceus.__main__ import main
    regions=tmp_path/'regions.json';regions.write_text(json.dumps([{'page':1,'bbox':[0,0,500,450],'caption':'Energy resolution'}]))
    monkeypatch.setattr('sys.argv',['lynceus','ingest',str(config.DATA/'sources'/sid/'source.pdf'),'--public','--title','CLI reference','--source-uri','https://example.org/reference.pdf','--regions',str(regions)])
    main()
    with store.db() as c:
        source=store.unpack(c.execute("SELECT * FROM sources WHERE title='CLI reference'").fetchone())
        job=store.unpack(c.execute('SELECT * FROM jobs WHERE source_id=?',(source['id'],)).fetchone())
    from lynceus.policy import Protection
    assert source['record']['protection']==Protection.model_validate(public_protection()).model_dump()
    assert source['record']['source']['uri']=='https://example.org/reference.pdf'
    assert job['kind']=='auto' and job['state']=='queued'
    worker.execute(job)
    with store.db() as c:result=c.execute("SELECT method,status FROM attempts WHERE plot_id=? ORDER BY created_at",(job['plot_id'],)).fetchall()
    assert [(r['method'],r['status']) for r in result]==[('vector','candidate'),('raster','candidate')]

def test_vector_first_and_raster_are_distinct_candidates_with_layers(setup):
    pid,sid,owner,builder,attempts=setup
    assert [a['method'] for a in attempts]==['vector','raster']
    a=attempts[0];assert a['status']=='candidate'
    assert len(a['diagnostics']['overlays'])==3
    assert {o['id'] for o in a['diagnostics']['overlays']}=={'samples','axes','masks'}
    assert a['diagnostics']['runner']['platform']=='Linux'
    data=json.loads((config.DATA/'attempts'/a['id']/'data.plot.json').read_text())
    measured=data['panels'][0]['series'][0]
    assert measured['x']==pytest.approx([1,2,3],abs=.002)
    assert measured['y']==pytest.approx([.8,.5,.3],abs=.002)
    review.verify(a)
    assert attempts[1]['status']=='candidate'

def test_exact_version_signoff_selection_export_and_discard(setup):
    pid,sid,owner,builder,attempts=setup;a,b=attempts
    with TestClient(app) as client:
        headers=auth(client,'demo_dev')
        body={'snapshot_sha256':a['snapshot_sha256'],'decision':'approve','reason':'Verified fixture','checks':sorted(review.CHECKS)}
        assert client.post(f"/api/attempts/{a['id']}/decision",json=body,headers=headers).status_code==403
        headers=auth(client)
        assert client.post(f"/api/attempts/{a['id']}/decision",json=body|{'checks':[]},headers=headers).status_code==400
        assert client.post(f"/api/attempts/{a['id']}/decision",json=body|{'snapshot_sha256':'stale'},headers=headers).status_code==400
        assert client.post(f'/api/plots/{pid}/export',headers=headers).status_code==405
        assert client.get(f'/api/plots/{pid}/export').status_code==400
        assert client.post(f"/api/attempts/{a['id']}/decision",json=body,headers=headers).status_code==200
        for attempt in [b,a]:
            response=client.post(f"/api/attempts/{attempt['id']}/decision",json=body|{'snapshot_sha256':attempt['snapshot_sha256']},headers=headers);assert response.status_code==200,response.text
        export=client.get(f'/api/plots/{pid}/export');assert export.status_code==200
        with zipfile.ZipFile(io.BytesIO(export.content)) as z:
            bundle=json.loads(z.read('bundle.json'));assert bundle['attempt']['id']==a['id']
            assert bundle['requires_document_signoff'] is True
            assert digest(json.loads(z.read('manifest.json')))==a['snapshot_sha256']
            assert 'samples.png' in z.namelist() and 'original.png' in z.namelist()
        from lynceus.client import read_bundle
        source=store.source(sid,owner)
        imported=read_bundle(io.BytesIO(export.content),source['pdf_sha256'],source['record']['protection'])
        assert imported['plot_data']['schema']=='daedalus.plot/v1' and imported['requires_document_signoff']
        with pytest.raises(ValueError,match='policy mismatch'):read_bundle(io.BytesIO(export.content),source['pdf_sha256'],{})
        assert client.post(f"/api/attempts/{a['id']}/decision",json=body|{'decision':'discard'},headers=headers).status_code==200
        assert client.get(f"/api/attempts/{a['id']}/artifacts/data.plot.json").status_code==404
        assert client.get(f'/api/plots/{pid}/image').status_code==200
        assert client.get(f'/api/plots/{pid}/export').status_code==400
        assert (config.DATA/'.history'/a['id']/'manifest.json').is_file()
        assert not (config.DATA/'.history'/a['id']/'data.plot.json').exists()
        assert not (config.DATA/'attempts'/a['id']/'data.plot.json').exists()
        assert client.get(f"/api/attempts/{b['id']}/artifacts/data.plot.json").status_code==200

def test_tamper_rejected_and_proposal_limits(setup):
    pid,sid,owner,builder,attempts=setup;a=attempts[0]
    path=config.DATA/'attempts'/a['id']/'data.plot.json';path.write_text('{}')
    with pytest.raises(ValueError,match='artifact changed'):review.decide(a['id'],owner,a['snapshot_sha256'],'approve','Checked',review.CHECKS)
    p=json.loads((config.DATA/'attempts'/a['id']/'proposal.json').read_text());p['x']['v2']=p['x']['v1']
    with pytest.raises(ValueError):Proposal.model_validate(p)
    p=json.loads((config.DATA/'attempts'/a['id']/'proposal.json').read_text());p['series'][0]['points'][0]=[9999,9999]
    from PIL import Image
    width,height=Image.open(config.DATA/'plots'/pid/'original.png').size
    with pytest.raises(ValueError,match='outside'):Proposal.model_validate(p).check_image(width,height)

def test_agent_queue_materialization_and_replay_protection(setup):
    pid,sid,owner,builder,attempts=setup;a=attempts[0]
    proposal=json.loads((config.DATA/'attempts'/a['id']/'proposal.json').read_text())
    with TestClient(app) as client:
        headers=auth(client)
        response=client.post(f'/api/plots/{pid}/agent',json={'message':'Check and retain these measured points','parent_attempt_id':a['id']},headers=headers);assert response.status_code==201
        token=(config.DATA/'.agent-token').read_text().strip();agent_headers={'authorization':'Bearer '+token}
        claim=client.get('/api/agent/claim',headers=agent_headers);assert claim.status_code==200
        jid=claim.json()['job']['id'];assert claim.json()['context']['proposal']['series']
        body={'reply':'Verified fixture coordinates; still needs human sign-off.','proposal':proposal,'harness':{'model':'fixture'}}
        assert client.post(f'/api/agent/jobs/{jid}/complete',json=body,headers=agent_headers).status_code==200
        assert client.post(f'/api/agent/jobs/{jid}/complete',json=body,headers=agent_headers).status_code==400
        job=store.claim('test-runner');assert job['kind']=='materialize-agent';worker.execute(job);store.finish(job['id'])
        result=client.get(f'/api/plots/{pid}').json();agent=result['attempts'][-1]
        assert agent['method']=='agent' and agent['status']=='candidate'
        assert agent['parent_id']==a['id'] and result['selected_attempt'] is None
        assert result['messages'][-1]['role']=='assistant'
        discarded=client.post(f"/api/attempts/{agent['id']}/decision",json={'snapshot_sha256':agent['snapshot_sha256'],'decision':'discard','reason':'Discard test proposal','checks':[]},headers=headers)
        assert discarded.status_code==200
        with store.db() as c:payload=json.loads(c.execute('SELECT payload FROM jobs WHERE id=?',(job['id'],)).fetchone()[0])
        assert 'proposal' not in payload

def test_permissions_csrf_and_one_dimensional_scope(setup):
    pid,sid,owner,builder,attempts=setup
    with TestClient(app) as client:
        assert client.get('/api/catalog').status_code==401
        headers=auth(client)
        assert client.post(f'/api/plots/{pid}/run',json={'method':'raster'}).status_code==403
        assert client.post(f'/api/plots/{pid}/run',json={'method':'raster'},headers=headers|{'origin':'https://outside.example'}).status_code==403
        with store.db() as c:c.execute("UPDATE plots SET caption='Equipotential contour field map',scope='pending' WHERE id=?",(pid,))
        worker.execute({'id':None,'kind':'auto','plot_id':pid,'source_id':sid,'payload':{}})
        assert store.plot(pid,owner)['scope']=='out_of_scope'
        a=attempts[0]
        with pytest.raises(ValueError,match='1-D scope'):review.decide(a['id'],owner,a['snapshot_sha256'],'approve','Checked',review.CHECKS)

def test_private_source_not_visible_or_sent_to_agent(setup):
    pid,sid,owner,builder,attempts=setup
    policy={'classification':'internal','audience':{'any_of':[{'type':'group','id':'group:other'}]},'requirements':{'all_of':[]}}
    with store.db() as c:
        source=store.unpack(c.execute('SELECT * FROM sources WHERE id=?',(sid,)).fetchone());source['record']['protection']=policy;c.execute('UPDATE sources SET record=? WHERE id=?',(canonical(source['record']),sid))
    with TestClient(app) as client:
        headers=auth(client)
        assert client.get(f'/api/plots/{pid}').status_code==404
        catalog=client.get('/api/catalog').json()
        assert catalog['sources']==[] and catalog['attempt_counts']==[] and catalog['jobs']==[]
        assert client.post(f'/api/plots/{pid}/agent',json={'message':'Extract'},headers=headers).status_code==404

def test_raster_only_ocr_and_saturated_colour_curve(setup):
    pid,sid,owner,builder,attempts=setup
    proposal,evidence=raster.extract(config.DATA/'plots'/pid/'original.png','Raster-only reference')
    assert evidence['calibration_source']=='raster OCR'
    assert len(evidence['ocr_anchors']['x'])>=3 and len(evidence['ocr_anchors']['y'])>=3
    assert proposal.x.scale==proposal.y.scale=='linear'
    assert any(s.kind=='curve' for s in proposal.series)
    assert any(s.kind=='markers' and len(s.points)==3 for s in proposal.series)
