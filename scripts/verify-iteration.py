#!/usr/bin/env python3
"""Local impact checks. Product CI and merge/release retain full regression."""
import argparse,json,os,shlex,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT/'.ai/managed'))

def changed_paths(base):
    paths=set()
    # Git diff against the chosen baseline includes staged + unstaged changes;
    # --no-renames includes both sides of rename/deletion. Untracked files count.
    for command in [['git','diff','--no-renames','--name-only','-z',base],['git','ls-files','--others','--exclude-standard','-z']]:
        output=subprocess.check_output(command,cwd=ROOT)
        paths.update(filter(None,output.decode().split('\0')))
    return sorted(paths)

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',default='HEAD')
    parser.add_argument('--intent',choices=['draft','ready_for_review','merge_candidate','release_candidate'],default='draft')
    parser.add_argument('--plan-only',action='store_true')
    args=parser.parse_args(argv)
    try:
        changed=changed_paths(args.base)
    except (OSError,subprocess.CalledProcessError,UnicodeError):
        changed=['.github/workflows/unknown-impact'] # full fail-closed, never prose exemption
    try:
        from ai_ops_kit.gates.verification_tiers import select_tests
        from ai_ops_kit.shared.project_detector import detect
        profile=detect(ROOT)
        selection=select_tests(changed,str(ROOT),lifecycle_intent=args.intent,profile=profile)
        if not changed: # no evidence of a change is no test qualification
            selection={'tier':'full','full_command':True,'note':'No changed paths: full verification'}
        if selection['tier']=='skip':
            commands=['npm run lint'] # Keep non-product checks for allowlisted prose
        else:
            commands=['npm run check','npm run lint','npm run build']
            targeted=selection.get('targeted_command')
            if targeted and any(c in targeted for c in ('&',';','|','$','`','\n','>','<')):
                selection={'tier':'full','full_command':True,'note':'Compound targeted command: full configured suite'}
                targeted=None
            commands.append(targeted or 'npm run test')
    except Exception as error:
        selection={'tier':'full','full_command':True,'note':f'Runtime unavailable: {type(error).__name__}'}
        commands=['npm run check','npm run lint','npm run build','npm run test']
    summary={key:value for key,value in selection.items() if key != 'changed_files'}
    print(json.dumps({'changed_files':changed[:30],'changed_count':len(changed),'selection':summary,'commands':commands},ensure_ascii=False),flush=True)
    if args.plan_only:return 0
    for command in commands:
        start=time.perf_counter()
        result=subprocess.run(shlex.split(command),cwd=ROOT,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
        print(json.dumps({'command':command,'seconds':time.perf_counter()-start,'exit_code':result.returncode}),flush=True)
        if result.returncode:return result.returncode if result.returncode>0 else 1
    return 0

if __name__=='__main__':sys.exit(main())
