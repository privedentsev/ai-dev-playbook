import sys,json,platform
sys.path.insert(0,'.')
from tests.test_checks_e2e import NoGitChecksE2ECase,_basis,_stub_catalog
x=NoGitChecksE2ECase(); x.setUp()
try:
 x.create_card('DEV-99')
 for n in 'abc': x.write(f'src/{n}/v.txt','v1')
 p={'schema_version':1,'policy_id':'chain','revision':1,'project_id':'no-git-shop','trust_mode':'local','approved_basis':_basis(),'subsystems':[{'id':'core','title':'Core'}], 'modules':[{'id':n,'title':n,'subsystem_id':'core','paths':[f'src/{n}/**']} for n in 'abc']+[{'id':'meta','title':'Meta','subsystem_id':'core','paths':['ai-dev-playbook/**','docs/**','AGENTS.md','CHANGELOG.md']}], 'checks':[{'id':n,'title':n,'purpose':n,'kind':'command','modules':[n],'argv':x.command_argv(n),'timeout_seconds':10} for n in 'ab'], 'dependencies':[{'consumer':'check:a','dependency':'check:b','kind':'api','source':'explicit','evidence':'declared chain'},{'consumer':'check:b','dependency':'module:c','kind':'api','source':'explicit','evidence':'declared input'}], 'excludes':[], 'environment':{'toolchain':[{'id':'python','version':platform.python_version()}],'external':[]},'fallback':{'expansion_order':['module','subsystem','project'],'approved_commands':[],'unknown_scope':'project','limits':{'max_seconds':100,'max_bytes':5000000}},'test_catalog':_stub_catalog()}
 x.write('checks-policy.json',json.dumps(p))
 print('SETUP',x.cli('setup','apply'))
 print('FIRST',x.cli('checks','run','DEV-99','--scope','project'))
 print('COUNTS BEFORE',x.counts('a','b'))
 x.write('src/c/v.txt','v2')
 print('PLAN',x.cli('checks','plan','DEV-99'))
 print('RUN',x.cli('checks','run','DEV-99','--scope','project','--mode','stale'))
 print('COUNTS AFTER',x.counts('a','b'))
 print('RELEASE',x.cli('checks','release-ready','DEV-99'))
finally: x.doCleanups()
