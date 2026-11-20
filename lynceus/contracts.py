"""The runner receives pixels/calibration, and exports DAEDALUS XY evidence."""
import hashlib
import json
import math
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def file_sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp');temp.write_text(canonical(value));temp.replace(path)

class Strict(BaseModel):model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
class Axis(Strict):
    p1:float;v1:float;p2:float;v2:float
    scale:Literal['linear','log10']='linear'
    @model_validator(mode='after')
    def valid(self):
        if abs(self.p2-self.p1)<1 or self.v1==self.v2:raise ValueError('Axis anchors need distinct pixel positions and values')
        if self.scale=='log10' and min(self.v1,self.v2)<=0:raise ValueError('Log axes require positive anchor values')
        return self
    def value(self,p):
        a,b=(math.log10(self.v1),math.log10(self.v2)) if self.scale=='log10' else (self.v1,self.v2)
        value=a+(p-self.p1)/(self.p2-self.p1)*(b-a)
        if self.scale=='log10':
            if abs(value)>100:raise ValueError('Log calibration exceeds the supported numerical range')
            value=10**value
        if not math.isfinite(value) or abs(value)>1e30:raise ValueError('Extreme or non-finite calibration')
        return value
class Label(Strict):label:str=Field(max_length=240);unit:str=Field(default='',max_length=120)
class SampleSeries(Strict):
    label:str=Field(min_length=1,max_length=240)
    kind:Literal['markers','curve']
    color:str=Field(default='#e5531a',pattern=r'^#[0-9a-fA-F]{6}$')
    points:list[tuple[float,float]]=Field(min_length=1,max_length=20000)
    error_caps:list[tuple[float,float]|None]=Field(default_factory=list,max_length=20000)
class Proposal(Strict):
    schema:Literal['lynceus.pixel-proposal/v1']='lynceus.pixel-proposal/v1'
    title:str=Field(min_length=1,max_length=240)
    frame:tuple[float,float,float,float]
    x:Axis;y:Axis
    x_label:Label=Field(default_factory=lambda:Label(label='X - verify label'))
    y_label:Label=Field(default_factory=lambda:Label(label='Y - verify label'))
    series:list[SampleSeries]=Field(min_length=1,max_length=128)
    masks:list[tuple[float,float,float,float]]=Field(default_factory=list,max_length=100)
    warnings:list[str]=Field(default_factory=list,max_length=100)
    @model_validator(mode='after')
    def valid(self):
        l,t,r,b=self.frame
        if not (0<=l<r and 0<=t<b):raise ValueError('Invalid plot frame')
        if sum(len(s.points) for s in self.series)>100000:raise ValueError('Too many samples')
        return self
    def check_image(self,width,height):
        l,t,r,b=self.frame
        if r>width+1 or b>height+1:raise ValueError('Frame exceeds the source image')
        for axis,maximum in ((self.x,width),(self.y,height)):
            if min(axis.p1,axis.p2)<-1 or max(axis.p1,axis.p2)>maximum+1:raise ValueError('Calibration anchors exceed the image')
        for series in self.series:
            for x,y in series.points:
                if not (l-1<=x<=r+1 and t-1<=y<=b+1):raise ValueError('A sample is outside the plot frame')
        for a,c,d,e in self.masks:
            if not (0<=a<d<=width+1 and 0<=c<e<=height+1):raise ValueError('Mask exceeds source image')

def numerical(proposal):
    series=[];bounds={}
    for i,s in enumerate(proposal.series):
        entry={'id':f'series-{i+1}','label':s.label,'kind':'xy','x':[proposal.x.value(x) for x,y in s.points],'y':[proposal.y.value(y) for x,y in s.points]}
        if len(s.error_caps)==len(s.points) and all(s.error_caps):
            pairs=[sorted([proposal.y.value(a),proposal.y.value(b)]) for a,b in s.error_caps]
            bounds[entry['id']]=[{'lower':a,'upper':b} for a,b in pairs]
            if all(a<=y<=b and abs((a+b)/2-y)<=max((b-a)*.06,1e-10) for (a,b),y in zip(pairs,entry['y'])):
                entry['y_error']=[(b-a)/2 for a,b in pairs]
        series.append(entry)
    result={'schema':'daedalus.plot/v1','title':proposal.title,'panels':[{'id':'extracted','title':proposal.title,'x':proposal.x_label.model_dump(),'y':proposal.y_label.model_dump(),'series':series}]}
    return result,bounds
