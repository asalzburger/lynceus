"""Metis integration: verify selected, signed-off extraction bundles before import.

No Metis database or publication decisions are changed by this module.
"""
import io
import json
import zipfile
from .contracts import digest,file_sha

def read_bundle(path,expected_pdf_sha256,expected_protection):
    with zipfile.ZipFile(path) as archive:
        if sum(i.file_size for i in archive.infolist())>128*1024**2:raise ValueError('Extraction bundle exceeds 128 MiB')
        if len(archive.namelist())!=len(set(archive.namelist())):raise ValueError('Duplicate bundle filenames')
        if any('/' in n or '\\' in n or n in {'.','..'} for n in archive.namelist()):raise ValueError('Unsafe bundle filename')
        envelope=json.loads(archive.read('bundle.json'));manifest=json.loads(archive.read('manifest.json'))
        if envelope['schema']!='lynceus.metis-bundle/v1':raise ValueError('Unknown extraction bundle schema')
        attempt=envelope['attempt'];decision=envelope['decision'];source=envelope['source']
        if attempt['status']!='approved' or decision['decision']!='approve' or decision['attempt_id']!=attempt['id']:raise ValueError('An exact extraction sign-off is required')
        if digest(manifest)!=attempt['snapshot_sha256'] or decision['snapshot_sha256']!=attempt['snapshot_sha256']:raise ValueError('Decision and extraction snapshot do not match')
        if source['pdf_sha256']!=expected_pdf_sha256 or manifest['source_pdf_sha256']!=expected_pdf_sha256:raise ValueError('Source PDF checksum mismatch')
        if source['record']['protection']!=expected_protection or manifest['protection']!=expected_protection:raise ValueError('Inherited source policy mismatch')
        import hashlib
        artifacts={}
        for name,value in manifest['artifacts'].items():
            data=archive.read(name)
            if hashlib.sha256(data).hexdigest()!=value:raise ValueError('Extraction artifact checksum mismatch: '+name)
            artifacts[name]=data
        original=archive.read('original.png')
        if hashlib.sha256(original).hexdigest()!=manifest['source_crop_sha256']:raise ValueError('Source crop checksum mismatch')
        numerical=json.loads(artifacts['data.plot.json'])
        if numerical.get('schema')!='daedalus.plot/v1' or set(numerical)!={'schema','title','panels'}:raise ValueError('Expected DAEDALUS numerical plot contract')
        return {'source':source,'figure':envelope['figure'],'attempt':attempt,'decision':decision,'manifest':manifest,'plot_data':numerical,'original_png':original,'artifacts':artifacts,'requires_document_signoff':True}
