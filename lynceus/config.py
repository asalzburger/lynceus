import os
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
DATA=Path(os.environ.get('LYNCEUS_DATA',ROOT/'var')).resolve()
def database_path():
    return Path(os.environ.get('LYNCEUS_DB',str(DATA/'lynceus.sqlite3')))
def prepare():
    for name in ('sources','plots','attempts','.history','uploads','exports'): (DATA/name).mkdir(parents=True,exist_ok=True)
