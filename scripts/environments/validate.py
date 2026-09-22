#!/usr/bin/env python3
"""Validate local Compose and render preserved production values without deployment."""
import argparse,json,os,re,shlex,subprocess
from pathlib import Path
import yaml
from local_dev import compose

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    root=Path.cwd();contract=json.loads((root/'environments/environment-contract.json').read_text())
    if contract.get('runtime')!='local-compose':raise ValueError('Expected local dev runtime')
    args.output.mkdir(parents=True,exist_ok=True);helm=shlex.split(os.getenv('HELM_COMMAND','helm'));count=0
    for name,cfg in contract['services'].items():
        metadata=yaml.safe_load((root/cfg['devValues']).read_text())
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',metadata['image'].get('digest','')):raise ValueError('Mutable dev image')
        if set(metadata)-{'image','schemaMigration'}:raise ValueError('Dev image metadata must not contain runtime credentials or Kubernetes settings')
        rendered=subprocess.check_output([*helm,'template',name,cfg['chart'],'--namespace','pawbridge','--values',cfg['prodValues']],text=True)
        (args.output/('prod-'+name+'.yaml')).write_text(rendered);count+=1
    (args.output/'compose.yaml').write_text(yaml.safe_dump(compose(),sort_keys=False))
    print(json.dumps({'productionChartsRendered':count,'localComposeGenerated':True,'liveApplied':False}))
if __name__=='__main__':main()
