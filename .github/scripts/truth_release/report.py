"""Assemble the release reader's canonical report from published rows and changed module ZIPs."""

from contextlib import ExitStack
import json
import mmap
import os
from pathlib import Path
import re
import sys
import zipfile


PREFIX = b'{"modules": ['
# Skip JSON strings whole, including escaped quotation marks and braces in Lean names.
TOKENS = re.compile(rb'"(?:[^"\\]++|\\.)*+"|[{}]')


def reused_spans(data, suffix, wanted):
    """Locate canonical top-level module rows without decoding their declarations."""
    if data[:len(PREFIX)] != PREFIX or data[-len(suffix):] != suffix:
        raise ValueError("incompatible predecessor raw Lean report schema or encoding")
    end = len(data) - len(suffix)
    start = len(PREFIX)
    depth = 0
    name = None
    spans = {}
    for token in TOKENS.finditer(data, start, end):
        pos = token.start()
        if data[pos] == ord('{'):
            if not depth and pos != start:
                raise ValueError("invalid predecessor module boundary")
            depth += 1
        elif data[pos] == ord('}'):
            depth -= 1
            if depth == 0:
                if name is None:
                    raise ValueError("predecessor row has no module")
                if name in wanted:
                    if name in spans:
                        raise ValueError(f"duplicate predecessor module {name}")
                    spans[name] = (start, token.end())
                name = None
                start = token.end()
                if start < end:
                    if data[start:start + 2] != b', ':
                        raise ValueError("invalid predecessor module delimiter")
                    start += 2
        elif depth == 1 and data[pos:pos + 10] == b'"module": ':
            value = TOKENS.match(data, pos + 10, end)
            if value is None or data[value.start()] != ord('"'):
                raise ValueError("invalid predecessor module name")
            name = json.loads(value.group())
    if depth or start != end or spans.keys() != wanted:
        raise ValueError("predecessor report lacks the requested module rows")
    return spans


def write_changed_row(artifact, name, output, materials, publication):
    # The issuer reads statement identities from the raw row. The source-pinned
    # Inspector owns the module ZIP format, its provenance and canonical encoding.
    with zipfile.ZipFile(artifact) as bundle:
        members = publication.bundle_members(bundle, ('', '.materials.zip', '.provenance.json'))
        def read(suffix):
            with publication.open_zip_member(bundle, members[publication.RAW + suffix]) as stream:
                return publication.read_json(stream.read())
        module = materials.require_keys(read(''), {'schema', 'modules'}, 'native module report')
        if module['schema'] != materials.REPORT_SCHEMA or len(module['modules']) != 1:
            raise ValueError(f"invalid native module report for {name}")
        row, = module['modules']
        publication.require_module_keys(row)
        if row['module'] != name:
            raise ValueError(f"native module report does not match {name}")
        publication.check_origin(read('.provenance.json'), row, check_report=False)
        output.write(materials.canonical_json(row)[:-1])


def assemble(root, destination):
    sys.path.insert(0, str(root / "tools/lean-inspector"))
    import materials
    import native
    import publication

    modules = sorted(publication.selection.Selection(root).modules())
    if not modules:
        raise ValueError("the configured report has no modules")
    artifacts = native.state(root) / "modules"
    suffix = b'], "schema": ' + materials.canonical_json(materials.REPORT_SCHEMA)[:-1] + b'}\n'
    base = os.environ.get('TRUTH_RELEASE_BASE')
    spans = {}
    with ExitStack() as stack:
        if base and (reuse_file := Path(base) / 'reuse.json').is_file():
            names = json.loads(reuse_file.read_bytes())
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                raise ValueError("expected a list of module names in reuse.json")
            wanted = set(names)
            if len(wanted) != len(names) or not wanted.issubset(modules):
                raise ValueError("unexpected reused module")
            if wanted:
                source = stack.enter_context((Path(base) / 'raw-lean-report.json').open('rb'))
                data = stack.enter_context(mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ))
                spans = reused_spans(data, suffix, wanted)
                view = stack.enter_context(memoryview(data))
        with destination.open("xb") as report:
            report.write(PREFIX)
            for index, name in enumerate(modules):
                if index:
                    report.write(b", ")
                if name in spans:
                    start, end = spans[name]
                    report.write(view[start:end])
                else:
                    write_changed_row(artifacts / (name + ".zip"), name, report, materials, publication)
            report.write(suffix)
    print(f"Prepared {len(modules)} modules ({len(spans)} reused) in {destination.stat().st_size} bytes", flush=True)


if __name__ == "__main__":
    assemble(Path.cwd(), Path(sys.argv[1]))
