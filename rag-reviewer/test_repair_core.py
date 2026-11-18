import copy
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'paradedb-personal-rag'))
from reviewer_core import ReviewerStore, ReviewError, sha256_file
from repair_core import RepairManager, CONFIRM, overlap


def fixture():
    bbox = {'l':0,'r':100,'t':100,'b':0,'coord_origin':'BOTTOMLEFT'}
    return {'schema_name':'DoclingDocument', 'body':{'children':[{'$ref':'#/texts/0'},{'$ref':'#/texts/1'}]},
        'pages':{'1':{}, '2':{}}, 'texts':[
            {'self_ref':'#/texts/0','parent':{'$ref':'#/body'},'children':[], 'label':'text',
             'text':'A T TO R N E YS A T L AW','orig':'A T TO R N E YS A T L AW',
             'prov':[{'page_no':1,'bbox':bbox,'charspan':[0,23]}]},
            {'self_ref':'#/texts/1','parent':{'$ref':'#/body'},'children':[], 'label':'text',
             'text':'Useful reference content remains.','prov':[{'page_no':2,'bbox':bbox}]}]}


class Repairs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ReviewerStore(self.tmp.name)
        self.store.ensure_layout()
        self.path = self.store.incoming / 'test/book.json'
        self.path.parent.mkdir()
        self.path.write_text(json.dumps(fixture()))
        self.original = self.path.read_bytes()
        self.digest = sha256_file(self.path)
        self.manager = RepairManager(self.store)
        self.addCleanup(lambda:self.manager.pool.shutdown(wait=True))

    def prepare(self):
        return self.manager.prepare('test/book.json', self.digest)

    def apply(self, action='replace', text='ATTORNEYS AT LAW'):
        return self.manager.apply(self.digest, [{'ref':'#/texts/0','action':action,'text':text}], 'Verified source', CONFIRM)

    def test_prepare_collects_all_findings_without_writes_to_input(self):
        batch = self.prepare()
        self.assertEqual(len(batch['items']),1)
        self.assertEqual(self.path.read_bytes(),self.original)
        self.assertIsNone(batch['source'])

    def test_verified_preserves_original_and_requires_fresh_review(self):
        import parse_check
        self.prepare()
        result = self.apply('verify', 'THIS CANDIDATE MUST NOT BE USED')
        document = json.loads(self.path.read_text())
        self.assertEqual(document['texts'], fixture()['texts'])
        self.assertEqual(parse_check.assess_file(self.path)['status'], 'FAIL')
        preflight = self.store.preflight('test/book.json', result['sha256'])
        self.assertEqual(preflight['status'], 'PASS')
        self.assertEqual(preflight['verified_correct'], ['#/texts/0'])
        self.assertFalse((self.store.reviews / (result['sha256'] + '.json')).exists())
        with self.assertRaises(ReviewError):
            self.store.record_review('test/book.json', result['sha256'], {
                'deterministic_status':'PASS', 'model_status':'FAIL', 'final_status':'PASS'})
        self.store.record_review('test/book.json', result['sha256'], {
            'deterministic_status':'PASS', 'model_status':'PASS', 'final_status':'PASS'})

    def test_verified_invalidated_by_any_document_change(self):
        self.prepare()
        self.apply('verify')
        document = json.loads(self.path.read_text())
        document['texts'][1]['text'] += ' changed'
        self.path.write_text(json.dumps(document))
        self.assertEqual(self.store.preflight('test/book.json')['status'], 'FAIL')

    def test_embedded_acceptance_is_not_authority(self):
        document = fixture()
        document['rag_curation'] = {'verified_correct': ['#/texts/0']}
        self.path.write_text(json.dumps(document))
        self.assertEqual(self.store.preflight('test/book.json')['status'], 'FAIL')

    def test_verified_does_not_hide_other_findings_and_survives_ref_remap(self):
        document = fixture()
        document['texts'][1]['text'] = document['texts'][0]['text']
        self.path.write_text(json.dumps(document))
        self.digest = sha256_file(self.path)
        self.prepare()
        self.manager.apply(self.digest, [
            {'ref':'#/texts/0','action':'keep'}, {'ref':'#/texts/1','action':'verify'}],
            'Checked second item only', CONFIRM)
        result = self.store.preflight('test/book.json')
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(len(result['findings']), 1)
        self.digest = sha256_file(self.path)
        self.prepare()
        # Any new derived document invalidates earlier receipts. Verify both
        # through a fresh all-findings batch for this remapping test.
        document.pop('rag_curation', None)
        document['name'] = 'Fresh remapping test'
        self.path.write_text(json.dumps(document))
        self.digest = sha256_file(self.path)
        self.prepare()
        self.manager.apply(self.digest, [
            {'ref':'#/texts/0','action':'exclude'}, {'ref':'#/texts/1','action':'verify'}],
            'Checked remaining item', CONFIRM)
        result = self.store.preflight('test/book.json')
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['verified_correct'], ['#/texts/0'])

    def test_changed_checker_reasons_invalidate_acceptance(self):
        from unittest.mock import patch
        from verified_findings import parse_check
        self.prepare()
        self.apply('verify')
        inspect = parse_check.inspect_item
        def changed(item):
            finding = inspect(item)
            if finding:
                finding.reasons.append('encoding:new-check')
            return finding
        with patch.object(parse_check, 'inspect_item', changed):
            self.assertEqual(self.store.preflight('test/book.json')['status'], 'FAIL')

    def test_report_has_explicit_verified_choice(self):
        self.prepare()
        report = (self.manager.directory(self.digest) / 'index.html').read_text()
        self.assertIn('value="verify"', report)
        self.assertIn('ignoring the replacement box', report)

    def test_replace_archives_and_preserves_orig_citations(self):
        self.prepare()
        result = self.apply()
        d = json.loads(self.path.read_text())
        self.assertEqual(d['texts'][0]['text'],'ATTORNEYS AT LAW')
        self.assertEqual(d['texts'][0]['orig'],'A T TO R N E YS A T L AW')
        self.assertEqual(d['texts'][0]['prov'][0]['page_no'],1)
        self.assertEqual((self.manager.directory(self.digest)/'original.json').read_bytes(),self.original)
        self.assertNotEqual(result['sha256'],self.digest)
        self.assertFalse((self.store.reviews/(result['sha256']+'.json')).exists())

    def test_item_exclusion_rewrites_reference_without_page_exclusion(self):
        self.prepare()
        self.apply('exclude')
        d = json.loads(self.path.read_text())
        self.assertEqual(len(d['texts']),1)
        self.assertEqual(d['texts'][0]['self_ref'],'#/texts/0')
        self.assertEqual(d['body']['children'],[{'$ref':'#/texts/0'}])
        self.assertEqual(set(d['pages']),{'1','2'})

    def test_cross_page_replace_refused_but_item_exclusion_allowed(self):
        d = fixture()
        d['texts'][0]['prov'].append({'page_no':2})
        self.path.write_text(json.dumps(d)); self.digest=sha256_file(self.path)
        self.prepare()
        with self.assertRaises(ReviewError): self.apply()
        self.apply('exclude')

    def test_stale_hash_and_confirmation_refused(self):
        self.prepare()
        with self.assertRaises(ReviewError):
            self.manager.apply(self.digest, [], 'yes','yes')
        self.path.write_text('{}')
        with self.assertRaises(ReviewError): self.apply()

    def test_unknown_duplicate_empty_decisions_refused(self):
        self.prepare()
        for decisions in [[],[{'ref':'#/texts/1','action':'exclude'}],
                          [{'ref':'#/texts/0','action':'exclude'}]*2]:
            with self.assertRaises(ReviewError):
                self.manager.apply(self.digest, decisions, 'reason', CONFIRM)
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_retry_missing_source_keeps_blocked(self):
        self.prepare()
        b=self.manager.retry(self.digest)
        self.assertEqual(b['status'],'ready')
        self.assertIn('No hash-bound',b['items'][0]['retry_status'])
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_hash_bound_source_retry_suggestion_is_never_applied(self):
        raw=self.store.documents/'raw/test/book.pdf'; raw.parent.mkdir(parents=True); raw.write_bytes(b'pdf')
        receipts=self.store.root/'conversions'; receipts.mkdir()
        (receipts/'one.json').write_text(json.dumps({'status':'succeeded','document':'test/book.pdf',
            'output_document':'test/book.json','output_sha256':self.digest,'sha256':sha256_file(raw)}))
        candidate=fixture(); candidate['texts'][0]['text']='ATTORNEYS AT LAW'
        self.manager.runner=lambda path,page:candidate
        self.prepare(); b=self.manager.retry(self.digest)
        self.assertEqual(b['items'][0]['candidate'],'ATTORNEYS AT LAW')
        self.assertEqual(b['items'][0]['retry_status'],'candidate-check-pass')
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_retry_error_is_durable(self):
        self.prepare()
        b=self.manager.retry(self.digest)
        self.assertEqual(self.manager.status(self.digest)['items'], b['items'])

    def test_html_does_not_interpret_document_markup(self):
        self.prepare()
        b=self.manager.status(self.digest); b['items'][0]['text']='</textarea><script>evil()</script>'
        self.manager.render(b)
        content=(self.manager.directory(self.digest)/'index.html').read_text()
        self.assertNotIn('<script>evil()',content)
        self.assertIn('&lt;script&gt;evil()',content)

    def test_bad_batch_id_rejected(self):
        with self.assertRaises(ReviewError):self.manager.status('../../else')

    def test_table_cell_changes_refused(self):
        d=fixture(); d['texts'][0]['parent']={'$ref':'#/tables/0'}
        self.path.write_text(json.dumps(d)); self.digest=sha256_file(self.path)
        self.prepare()
        for action in ['replace','exclude']:
            with self.assertRaisesRegex(ReviewError,'structured review'): self.apply(action)

    def test_running_batch_cannot_be_applied(self):
        b=self.prepare(); b['status']='retrying'; self.manager._save(b)
        with self.assertRaises(ReviewError): self.apply()

    def test_changed_raw_source_never_retried(self):
        raw=self.store.documents/'raw/test/book.pdf'; raw.parent.mkdir(parents=True); raw.write_bytes(b'pdf')
        receipts=self.store.root/'conversions'; receipts.mkdir()
        (receipts/'one.json').write_text(json.dumps({'status':'succeeded','document':'test/book.pdf',
            'output_document':'test/book.json','output_sha256':self.digest,'sha256':sha256_file(raw)}))
        self.prepare(); raw.write_bytes(b'changed')
        self.manager.runner=lambda *_:self.fail('changed PDF must never be retried')
        b=self.manager.retry(self.digest)
        self.assertIn('Raw PDF changed',b['items'][0]['retry_status'])

    def test_bbox_overlap(self):
        b=fixture()['texts'][0]['prov'][0]['bbox']
        self.assertEqual(overlap(b,b),1)
        self.assertEqual(overlap(b,{**b,'coord_origin':'TOPLEFT'}),0)


if __name__ == '__main__': unittest.main()
