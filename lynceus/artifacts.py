"""Render immutable attempt evidence inside the extraction runner."""
import json
import os
import platform
from PIL import Image,ImageDraw
from . import config,store
from .contracts import digest,file_sha,numerical,write

def persist(plot,source,method,proposal=None,evidence=None,error=None,parent=None,job_id=None):
    aid=store.uid();folder=config.DATA/'attempts'/aid;folder.mkdir()
    image_path=config.DATA/'plots'/plot['id']/'original.png'
    status='candidate' if proposal else 'deferred'
    diagnostics={'method':method,'scope':'xy-1d-candidate','requires_signoff':True,'warnings':proposal.warnings if proposal else [str(error or 'No supported numerical candidate')],'blocking':False,'runner':{'id':os.environ.get('LYNCEUS_RUNNER_ID','local-test'),'container_id':platform.node(),'image':os.environ.get('LYNCEUS_RUNNER_IMAGE','local-test'),'platform':platform.system()},'job_id':job_id,'overlays':[]}
    diagnostics['extractor_version']=(evidence or {}).get('version','lynceus.'+method+'/v1')
    if evidence:write(folder/'geometry.json',evidence)
    if proposal:
        width,height=Image.open(image_path).size;proposal.check_image(width,height)
        data,bounds=numerical(proposal);write(folder/'data.plot.json',data);write(folder/'proposal.json',proposal.model_dump())
        diagnostics.update(point_count=sum(len(s.points) for s in proposal.series),series_count=len(proposal.series),calibration={'x':proposal.x.model_dump(),'y':proposal.y.model_dump()},labels={'x':proposal.x_label.model_dump(),'y':proposal.y_label.model_dump(),'series':[s.label for s in proposal.series]},error_bar_bounds=bounds,series_kinds=[s.kind for s in proposal.series])
        layers={name:Image.new('RGBA',(width,height),(0,0,0,0)) for name in ['samples','axes','masks']}
        draw=ImageDraw.Draw(layers['samples'])
        for s in proposal.series:
            if s.kind=='curve' and len(s.points)>1:draw.line(s.points,fill=s.color,width=2)
            for x,y in s.points:draw.ellipse((x-3,y-3,x+3,y+3),outline=s.color,width=2)
            for (x,y),caps in zip(s.points,s.error_caps):
                if caps:
                    top,bottom=sorted(caps);draw.line((x,top,x,bottom),fill=s.color,width=2)
                    for edge in (top,bottom):draw.line((x-4,edge,x+4,edge),fill=s.color,width=2)
        draw=ImageDraw.Draw(layers['axes']);l,t,r,b=proposal.frame;draw.rectangle((l,t,r,b),outline='#087b92',width=2)
        for axis,name in [(proposal.x,'x'),(proposal.y,'y')]:
            for p,v in [(axis.p1,axis.v1),(axis.p2,axis.v2)]:
                x,y=(p,b) if name=='x' else (l,p)
                draw.rectangle((x-4,y-4,x+4,y+4),fill='#087b92');draw.text((max(1,min(x+5,width-150)),max(1,y-16)),f'{name}={v:.5g} {axis.scale}',fill='#006575',stroke_width=1,stroke_fill='white')
        draw=ImageDraw.Draw(layers['masks'])
        for box in proposal.masks:draw.rectangle(box,fill=(120,120,130,60),outline='#925280',width=2)
        base=Image.open(image_path).convert('RGBA')
        for name,layer in layers.items():
            layer.save(folder/(name+'.png'));diagnostics['overlays'].append({'id':name,'label':{'samples':'Samples / curves','axes':'Axis calibration','masks':'Excluded regions'}[name],'image':name+'.png'})
            Image.alpha_composite(Image.open(image_path).convert('RGBA'),layer).convert('RGB').save(folder/(name+'-overlay.png'))
            base=Image.alpha_composite(base,layer)
        base.convert('RGB').save(folder/'overlay.png')
        # Compatibility project for the current Metis calibration editor.
        project={'format':2,'image':str(image_path),'image_shape':[height,width],'frame':dict(left=l,right=r,top=t,bottom=b,has_left=True,has_right=True,has_top=True,has_bottom=True,confidence=0),'calibration':{a:getattr(proposal,a).model_dump()|{'fit':None} for a in ['x','y']},'warnings':proposal.warnings,'confidence':{'x_axis':.7,'y_axis':.7},'series':[]}
        for s,data_series in zip(proposal.series,data['panels'][0]['series']):
            project['series'].append({'name':s.label,'color':[229,83,26],'visible':True,'settings':{'mode':'scatter' if s.kind=='markers' else 'curve','x_step':2,'min_blob_fraction':.25,'max_gap':24,'smoothing':3,'max_points':0},'pixel_points':s.points,'data_points':list(zip(data_series['x'],data_series['y']))})
        write(folder/'project.pdproj',project)
    write(folder/'diagnostics.json',diagnostics)
    companion={'schema':'lynceus.extraction/v1','attempt_id':aid,'plot_id':plot['id'],'source':{'id':source['id'],'uri':source['record']['source']['uri'],'pdf_sha256':source['pdf_sha256'],'page':plot['page'],'bbox':plot['bbox'],'crop_sha256':file_sha(image_path)},'protection':source['record']['protection'],'tags':['extracted'] if proposal else [],'method':method,'parent_attempt_id':parent,'data_status':'candidate','physically_verified':False,'runner':diagnostics['runner']}
    write(folder/'provenance.json',companion)
    manifest={'schema':'lynceus.attempt/v1','attempt_id':aid,'plot_id':plot['id'],'method':method,'protection':source['record']['protection'],'source_pdf_sha256':source['pdf_sha256'],'source_crop_sha256':file_sha(image_path),'created_at':store.now(),'artifacts':{p.name:file_sha(p) for p in folder.iterdir() if p.is_file()}}
    write(folder/'manifest.json',manifest)
    with store.db() as c:c.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?)',(aid,plot['id'],method,status,parent,store.now(),json.dumps(manifest),digest(manifest),json.dumps(diagnostics)))
    return aid
