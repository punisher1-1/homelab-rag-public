"""Operator-written exceptions, never authorizations embedded in input JSON."""
import hashlib
import json

import parse_check


def binding(ref, item, finding):
    payload = {'ref': ref, 'item': item, 'finding': finding.as_dict()}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def accepted_items(reviews, identifier, digest, document):
    path = reviews / 'verified-correct' / (digest + '.json')
    if not path.is_file():
        return set()
    receipt = json.loads(path.read_text())
    if receipt.get('document') != identifier or receipt.get('sha256') != digest:
        return set()
    approved = {entry['binding'] for entry in receipt.get('items', [])}
    return {index for index, item in enumerate(document.get('texts', []))
            if (finding := parse_check.inspect_item(item)) is not None
            and binding(f'#/texts/{index}', item, finding) in approved}


def assess(reviews, identifier, digest, path):
    document = json.loads(path.read_text())
    accepted = accepted_items(reviews, identifier, digest, document)
    # check_file walks texts in order. Match by object identity in that same
    # ordered finding stream, rather than snippet (which may be duplicated).
    flags = [index in accepted for index, item in enumerate(document.get('texts', []))
             if parse_check.inspect_item(item) is not None]
    flags = iter(flags)
    result = parse_check.assess_file(path, accept_finding=lambda finding: next(flags))
    result['verified_correct'] = [f'#/texts/{index}' for index in sorted(accepted)]
    return result
