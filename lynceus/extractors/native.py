"""Native geometry adapter, including conservative unboxed legend masks."""
import re
import pymupdf
from . import vector
from ..contracts import Proposal

def unboxed_legends(page,region,frame,native):
    # Match multiple text rows to symbols immediately to their left. A lone
    # annotation is not sufficient evidence to remove nearby scientific points.
    rows=[]
    words=page.get_text('words')
    for w in words:
        rect=pymupdf.Rect(w[:4])
        if not frame.contains(rect) or not re.search('[A-Za-z]',w[4]) or len(w[4])<2:continue
        for series in native['series']:
            if series['kind']!='markers':continue
            for x,y in series['points']:
                if 1<w[0]-x<min(35,frame.width*.2) and abs(y-(w[1]+w[3])/2)<max(4,rect.height*.6):
                    rows.append((x,y,rect));break
    masks=[]
    for x,y,rect in rows:
        group=[r for r in rows if abs(r[0]-x)<3 and abs(r[1]-y)<max(35,rect.height*5)]
        ys={round(r[1],1) for r in group}
        if len(ys)<2:continue
        mask=pymupdf.Rect(min(r[0] for r in group)-5,min(r[1] for r in group)-rect.height*.7,max(r[2].x1 for r in group)+3,max(r[1] for r in group)+rect.height*.7)&frame
        if mask.get_area()<frame.get_area()*.3 and not any(mask.intersects(m) for m in masks):masks.append(mask)
    return masks

def extract(pdf,page_number,bbox,width,height,title):
    with pymupdf.open(pdf) as doc:
        page=doc[page_number-1];region=vector.figure_rect(bbox,page)
        paths=[(i,p) for i,p in enumerate(page.get_drawings()) if region.contains(p['rect'])]
        panels=[frame for _,frame in vector._frames(paths,region) if len(vector._ticks(paths,frame,'x'))>=3 and len(vector._ticks(paths,frame,'y'))>=3]
        if len(panels)>1:return None,{'version':vector.VERSION,'accepted':False,'reason_code':'multiple-panels','message':'More than one XY panel has tick marks; select and calibrate one panel explicitly.','panels':[list(r) for r in panels]}
    native=vector.recover(pdf,page_number,bbox)
    if not native['accepted']:return None,native
    region=pymupdf.Rect(native['figure_bbox_pdf']);frame=pymupdf.Rect(native['frame_bbox_pdf'])
    with pymupdf.open(pdf) as doc:extra=unboxed_legends(doc[page_number-1],region,frame,native)
    masks=[pymupdf.Rect(m) for m in native['legend_masks_pdf']]+extra
    def pixel(point):return [(point[0]-region.x0)/region.width*width,(point[1]-region.y0)/region.height*height]
    def axis(a,origin,extent,size):
        anchors=a['anchors'];lo,hi=anchors[0],anchors[-1]
        return {'p1':(lo['position']-origin)/extent*size,'v1':vector._value(a,lo['position']),'p2':(hi['position']-origin)/extent*size,'v2':vector._value(a,hi['position']),'scale':a['scale']}
    series=[]
    for s in native['series']:
        keep=[i for i,p in enumerate(s['points']) if not any(m.contains(pymupdf.Point(*p)) for m in masks)]
        if not keep:continue
        if s['kind']=='curve' and len(keep)!=len(s['points']):continue
        caps=[]
        for i in keep:
            c=s.get('error_caps',[])[i] if s.get('error_caps') else None
            caps.append(None if c is None else [pixel([0,c['top']['a'][1]])[1],pixel([0,c['bottom']['a'][1]])[1]])
        series.append({'label':s['label'],'kind':s['kind'],'color':'#'+''.join(f'{round(v*255):02x}' for v in s['color']),'points':[pixel(s['points'][i]) for i in keep],'error_caps':caps})
    if not series:return None,dict(native,accepted=False,message='No series survived legend masking')
    a,b=pixel([frame.x0,frame.y0]),pixel([frame.x1,frame.y1])
    warnings=native['warnings']+(['Additional unboxed legend regions were masked; check the mask overlay for accidental exclusions.'] if extra else [])
    result=Proposal.model_validate({'title':title[:240],'frame':[a[0],a[1],b[0],b[1]],'x':axis(native['axes']['x'],region.x0,region.width,width),'y':axis(native['axes']['y'],region.y0,region.height,height),'x_label':native['labels']['x'],'y_label':native['labels']['y'],'series':series,'masks':[[*pixel([m.x0,m.y0]),*pixel([m.x1,m.y1])] for m in masks],'warnings':warnings})
    native['additional_legend_masks_pdf']=[list(m) for m in extra]
    return result,native
