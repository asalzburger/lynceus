"""Pixel-based XY candidates. Missing/ambiguous calibration produces no numbers."""
import re
import cv2
import numpy as np
import pytesseract
from PIL import Image
from ..contracts import Proposal
from . import vector

VERSION='lynceus.raster/v2'
def frame_from_pixels(image):
    gray=cv2.cvtColor(image,cv2.COLOR_RGB2GRAY)
    dark=(gray<120).astype('uint8')*255
    h,w=gray.shape
    horizontal=cv2.morphologyEx(dark,cv2.MORPH_OPEN,np.ones((1,max(35,w//4)),np.uint8))
    vertical=cv2.morphologyEx(dark,cv2.MORPH_OPEN,np.ones((max(35,h//4),1),np.uint8))
    xs=np.flatnonzero((vertical>0).sum(axis=0)>h*.25)
    ys=np.flatnonzero((horizontal>0).sum(axis=1)>w*.25)
    if len(xs)<2 or len(ys)<2:raise ValueError('No supported rectangular raster XY frame')
    return [int(xs[0]),int(ys[0]),int(xs[-1]),int(ys[-1])]
def ocr_axis(image,frame,axis):
    l,t,r,b=map(int,frame);h,w=image.shape[:2]
    if axis=='x':left,top,right,bottom=max(0,l-20),b+2,min(w,r+25),min(h,b+55)
    else:left,top,right,bottom=max(0,l-90),max(0,t-10),max(1,l-2),min(h,b+15)
    crop=image[top:bottom,left:right]
    if crop.size==0:raise ValueError('Tick labels are outside the selected crop')
    gray=cv2.cvtColor(crop,cv2.COLOR_RGB2GRAY)
    scaled=cv2.resize(gray,None,fx=2,fy=2)
    data=pytesseract.image_to_data(scaled,config='--psm 6 -c tessedit_char_whitelist=0123456789.-+eE',output_type=pytesseract.Output.DICT,timeout=20)
    anchors=[]
    for i,text in enumerate(data['text']):
        value=vector._number(text.strip())
        if value is None or float(data['conf'][i])<35:continue
        p=(data['left'][i]+data['width'][i]/2)/2+left if axis=='x' else (data['top'][i]+data['height'][i]/2)/2+top
        anchors.append((p,value,text))
    anchors=sorted(anchors)
    if len(anchors)<3:raise ValueError(f'Raster {axis.upper()} needs at least three readable numeric labels')
    p=np.array([a[0] for a in anchors]);v=np.array([a[1] for a in anchors])
    if not (np.all(np.diff(v)>0) or np.all(np.diff(v)<0)):raise ValueError('OCR tick values are ambiguous or non-monotonic')
    fits=[]
    for scale in ['linear','log10']:
        if scale=='log10' and min(v)<=0:continue
        q=np.log10(v) if scale=='log10' else v
        slope,offset=np.polyfit(p,q,1)
        residual=float(np.max(np.abs((q-(slope*p+offset))/slope)))
        fits.append((residual,scale,slope,offset))
    residual,scale,slope,offset=min(fits)
    if residual>max(2.5,(r-l if axis=='x' else b-t)*.007):raise ValueError('OCR labels do not establish a consistent linear/log axis')
    def value(p):return float(10**(slope*p+offset) if scale=='log10' else slope*p+offset)
    return {'p1':float(p[0]),'v1':value(p[0]),'p2':float(p[-1]),'v2':value(p[-1]),'scale':scale},anchors

def extract(path,title,guide=None):
    image=np.asarray(Image.open(path).convert('RGB'));h,w=image.shape[:2]
    frame=guide.frame if guide else frame_from_pixels(image)
    axes={};evidence={'version':VERSION,'calibration_source':'native tick geometry' if guide else 'raster OCR','ocr_anchors':{}}
    for axis in ['x','y']:
        if guide:axes[axis]=getattr(guide,axis).model_dump()
        else:axes[axis],evidence['ocr_anchors'][axis]=ocr_axis(image,frame,axis)
    l,t,r,b=map(int,frame);roi=image[t+3:b-3,l+3:r-3]
    if roi.size==0:raise ValueError('Raster data region is empty')
    hsv=cv2.cvtColor(roi,cv2.COLOR_RGB2HSV)
    masks=[]
    color_bins=(hsv[:,:,0]//12)
    saturated=(hsv[:,:,1]>100)&(hsv[:,:,2]>20)
    for group,count in sorted(zip(*np.unique(color_bins[saturated],return_counts=True)),key=lambda pair:-pair[1])[:6]:
        if count>=12:masks.append((f'Colour {int(group)+1}',(saturated&(color_bins==group)).astype('uint8')*255))
    masks.append(('Dark marks',((roi.max(axis=2)<120)&(hsv[:,:,1]<90)).astype('uint8')*255))
    series=[];warnings=['Raster positions are estimates; check text/legend contamination, joined marks and missing error bars.']
    excluded=guide.masks if guide else []
    for name,mask in masks:
        for a,c,d,e in excluded:cv2.rectangle(mask,(max(0,int(a)-l-3),max(0,int(c)-t-3)),(int(d)-l-3,int(e)-t-3),0,-1)
        # Remove long grid/axis strokes before connected-component detection.
        for kernel in [np.ones((1,max(20,(r-l)//3)),np.uint8),np.ones((max(20,(b-t)//3),1),np.uint8)]:mask=cv2.subtract(mask,cv2.morphologyEx(mask,cv2.MORPH_OPEN,kernel))
        count,labels,stats,centres=cv2.connectedComponentsWithStats(mask)
        points=[];curves=[]
        for i in range(1,count):
            x,y,cw,ch,area=stats[i]
            if area<4:continue
            if 2<=cw<=30 and 2<=ch<=40 and .25<cw/ch<4:
                points.append([float(centres[i][0]+l+3),float(centres[i][1]+t+3)])
            elif cw>(r-l)*.18 and ch>2 and name!='Dark marks':
                if area>roi.shape[0]*roi.shape[1]*.12:
                    warnings.append('A large filled region was omitted; histogram/stacked-area bins are unsupported.');continue
                yy,xx=np.where(labels[y:y+ch,x:x+cw]==i);xy=[]
                for px in range(int(xx.min()),int(xx.max())+1,3):
                    values=yy[(xx>=px)&(xx<px+3)]
                    if len(values):xy.append([float(px+x+l+3),float(np.median(values)+y+t+3)])
                if len(xy)>3:curves.append(xy)
        if len(points)>=2:series.append({'label':name+' (raster blobs)','kind':'markers','color':'#e5531a','points':sorted(points)})
        for i,xy in enumerate(curves):series.append({'label':name+f' (raster trace {i+1})','kind':'curve','color':'#087b92','points':xy})
    if not series:raise ValueError('No supported raster marker groups or traces were recovered')
    proposal=Proposal.model_validate({'title':title[:240],'frame':frame,'x':axes['x'],'y':axes['y'],'x_label':guide.x_label.model_dump() if guide else {'label':'X - verify label','unit':''},'y_label':guide.y_label.model_dump() if guide else {'label':'Y - verify label','unit':''},'series':series,'masks':excluded,'warnings':warnings})
    proposal.check_image(w,h)
    return proposal,evidence
