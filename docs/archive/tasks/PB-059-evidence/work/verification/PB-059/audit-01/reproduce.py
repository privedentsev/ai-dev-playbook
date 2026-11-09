"""PB-059 independent audit probes. Run from repository root."""
import sys
import tempfile
from pathlib import Path
sys.path[:0] = [str(Path.cwd() / 'ai-dev-playbook/scripts'), str(Path.cwd() / 'tests')]
import checks_graph as cg
import checks_select as cs
from test_checks_select import make_policy, graph_with

def edge(consumer, dependency):
    return {'consumer': 'module:' + consumer, 'dependency': 'module:' + dependency,
            'kind': 'import', 'source': 'analyzer', 'analyzer_version': 'ast-v1',
            'evidence': 'fixture.py:1'}

policy = make_policy()
edges = [edge('checkout', 'payments'), edge('cart', 'checkout')]
selected = cs.select_checks(policy, changed=['src/payments/pay.py'], edges=edges, unknown=[])
print('A-01: chain payments -> checkout -> cart')
print({key: value['status'] for key, value in selected['checks'].items()})
print('Expected: all three run; actual cart-tests not_applicable')
print('\nA-02: closure digest with one reachable edge')
try:
    print(cs.closure_digest(edges, policy['checks'][1]))
except Exception as error:
    print(type(error).__name__ + ': ' + str(error))
print('\nA-03: dynamic import aliases')
for source in ['from importlib import import_module as load\nload(chosen)\n',
               'import importlib as il\nil.import_module(chosen)\n']:
    found = cg.analyze_python('src/cart/basket.py', source, {'src/cart/basket.py'})
    print(repr(source), found)
print('Expected: dynamic/unknown; actual no targets and no dynamic')
print('\nA-04: exceeded max_bytes')
policy['fallback']['limits']['max_bytes'] = 1
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / 'src/payments/pay.py'
    path.parent.mkdir(parents=True)
    path.write_text('x = 12345\n')
    plan = cs.plan(directory, policy, graph=graph_with([]))
    print({key: plan[key] for key in ['bytes_exceeded', 'budget_blocked', 'budget_reasons', 'summary']})
    print('Expected: budget blocked; actual run=3, blocked=0')
