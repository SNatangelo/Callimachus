import hashlib,json,zipfile
from pathlib import Path
import pefile
root=Path(__file__).resolve().parent
rows=[]
for wheel in sorted((root/'wheels').glob('*.whl')):
    binaries=[]
    with zipfile.ZipFile(wheel) as z:
        for name in z.namelist():
            if name.endswith(('.pyd','.dll','.exe')) and (not name.startswith('PyInstaller/bootloader/') or '/Windows-64bit-intel/' in name):
                pe=pefile.PE(data=z.read(name),fast_load=True)
                binaries.append({'path':name,'linker':f'{pe.OPTIONAL_HEADER.MajorLinkerVersion}.{pe.OPTIONAL_HEADER.MinorLinkerVersion}'})
    rows.append({'wheel':wheel.name,'sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),'binaries':binaries})
(root/'wheel-native-inventory.json').write_text(json.dumps(rows,indent=2)+'\n')
for row in rows:
    versions=sorted({x['linker'] for x in row['binaries']})
    print(row['wheel'],len(row['binaries']),','.join(versions))
p=root/'build-plan.json'
data=json.loads(p.read_text()); data['status']='building-native-components'; data['completed_wheels']=[r['wheel'] for r in rows]; p.write_text(json.dumps(data,indent=2)+'\n')
