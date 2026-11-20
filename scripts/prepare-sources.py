"""Copy the two public PDFs and existing Docling regions as runner inputs.

This preparation script does no numerical extraction and never changes Metis.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import uuid

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--metis',type=Path,default=Path(__file__).resolve().parents[2]/'metis');args=parser.parse_args()
    root=Path(__file__).resolve().parents[1];output=root/'var';output.mkdir(exist_ok=True)
    report=json.loads((args.metis/'var/build-report.json').read_text());sources=[]
    for item in report['documents']:
        folder=args.metis/'var/documents'/item['revision_id'];review=json.loads((folder/'review.json').read_text());manifest=json.loads((folder/'manifest.json').read_text())
        if review['record']['protection']['classification']!='public':raise ValueError('Seed preparation accepts the two public references only')
        sid=manifest['source_id'];target=output/'sources'/sid;target.mkdir(parents=True,exist_ok=True)
        expected=review['metadata']['file_sha256']
        if sha(folder/'source.pdf')!=expected:raise ValueError('Source PDF checksum mismatch')
        if not (target/'source.pdf').exists():shutil.copyfile(folder/'source.pdf',target/'source.pdf')
        if sha(target/'source.pdf')!=expected:raise ValueError('Prepared PDF checksum mismatch')
        boxes={f['image']:f['bbox'] for p in folder.glob('batch-*.json') for f in json.loads(p.read_text()).get('figures',[])}
        rows=[]
        for f in sorted(review['figures'],key=lambda x:(x['page'],x['image'])):
            pid=str(uuid.uuid5(uuid.UUID(sid),f['image']))
            destination=output/'plots'/pid;destination.mkdir(parents=True,exist_ok=True)
            if not (destination/'original.png').exists():shutil.copyfile(folder/f['image'],destination/'original.png')
            rows.append({'id':pid,'page':f['page'],'caption':f['caption'],'bbox':boxes[f['image']],'metis_revision_id':item['revision_id'],'metis_figure_id':f['id'],'source_crop_sha256':sha(folder/f['image'])})
        sources.append({'id':sid,'record':review['record'],'pdf_sha256':expected,'plots':rows})
    (output/'bootstrap.json').write_text(json.dumps(sources,indent=1));print('Prepared',len(sources),'public PDFs and',sum(len(s['plots']) for s in sources),'source figure regions')
if __name__=='__main__':main()
