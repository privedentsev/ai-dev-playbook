import sys, json
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
from test_compact_context import CompactContextTest, docs
from test_documents import tree_fingerprint
case = CompactContextTest()
case.setUp()
try:
    case.setup_external(False)
    original = case.path.read_text()
    # A fenced example in the header is ignored by both structural scanners.
    example = '~~~markdown\n' + docs.EXTERNAL_META + '\n~~~\n'
    case.path.write_text(original.replace(docs.EXTERNAL_META, example + docs.EXTERNAL_META, 1))
    hydrated = docs.read_text(case.path)
    print('hydration metadata parsed:', docs.parse_meta(hydrated, str(case.path), []))
    # Same visible Markdown with inline metadata is a supported control.
    sidecar = docs.meta_path(case.path)
    meta_bytes = sidecar.read_bytes()
    inline = original.replace(docs.EXTERNAL_META, docs.META_START + '\n```json\n' + meta_bytes.decode().rstrip() + '\n```\n' + docs.META_END)
    case.path.write_text(inline.replace(docs.META_START, example + docs.META_START, 1))
    sidecar.unlink()
    control = case.cli('card', 'show', 'DEV-1', '--full')
    print('inline control returncode:', control[0], 'example preserved:', example in control[1])
    sidecar.write_bytes(meta_bytes)
    case.path.write_text(original.replace(docs.EXTERNAL_META, example + docs.EXTERNAL_META, 1))
    before = tree_fingerprint(case.root)
    result = case.cli('card', 'show', 'DEV-1', '--full')
    print('fenced header marker:', repr(result))
    print('read_only:', before == tree_fingerprint(case.root))
    print('example preserved:', example in result[1])
    # An inline mention before the marker is also not the actual marker.
    case.path.write_text(original.replace('# DEV-1: test', '# DEV-1: example ' + docs.EXTERNAL_META))
    result = case.cli('card', 'show', 'DEV-1', '--json')
    print('title marker:', repr(result))
finally:
    case.tearDown()
