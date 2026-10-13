import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result
cfg = module('check_config')
snap = module('snapshot')

def valid():
    return {'schema_version':1, 'roles': {'controller':{'cli':'claude','model':'opus','extra_args':[]}, 'developer':{'cli':'claude','model':'sonnet','extra_args':[]}, 'reviewers':[{'id':'reviewer-a','cli':'omp','model':'test/model','extra_args':['--no-lsp'],'focus':['tests']}]}, 'workflow':{'max_fix_attempts_per_finding':3,'max_task_minutes':120,'wait_timeout_ms':600000,'model_diversity':'warn'}, 'verification':{'required_checks':[{'id':'tests','argv':['python','-m','unittest']}]},'safety':{'allow_stage':False,'allow_commit':False,'allow_push':False,'allow_merge':False}}

class ConfigTests(unittest.TestCase):
    def test_valid(self):
        cfg.validate_config(valid())
    def test_missing_config_no_writes(self):
        with tempfile.TemporaryDirectory() as t:
            before=list(Path(t).iterdir())
            result, code=cfg.load_config(t)
            self.assertEqual(code,3)
            self.assertEqual(result['status'],'CONFIG_REQUIRED')
            self.assertEqual(before,list(Path(t).iterdir()))
    def test_invalid_cases(self):
        mutations=[lambda c:c['roles'].update(reviewers=[]),lambda c:c['roles']['controller'].update(model=None),lambda c:c['roles']['reviewers'][0].update(model='fuzzy'),lambda c:c['roles']['reviewers'].append(copy.deepcopy(c['roles']['reviewers'][0])),lambda c:c['safety'].update(allow_commit=True),lambda c:c['workflow'].update(wait_timeout_ms=True),lambda c:c.update(unknown=1),lambda c:c['roles']['developer'].update(extra_args=['--permission-mode','bypassPermissions']),lambda c:c['verification'].update(required_checks=None),lambda c:c['verification']['required_checks'][0].update(argv=['git','commit']),lambda c:c['roles']['developer'].update(extra_args=['--model=override'])]
        for change in mutations:
            with self.subTest(change=change):
                data=valid();change(data)
                with self.assertRaises(cfg.ConfigError):cfg.validate_config(data)
    def test_template_intentionally_invalid(self):
        with self.assertRaises(cfg.ConfigError):
            cfg.validate_config(json.loads((ROOT/'assets/workflow.example.json').read_text()))
    def test_read_existing(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'.agent-pr';p.mkdir();(p/'workflow.json').write_text(json.dumps(valid()))
            self.assertEqual(cfg.load_config(t)[1],0)

class SnapshotTests(unittest.TestCase):
    def setup_scope(self,t):
        root=Path(t);task=root/'.agent-pr/tasks/test-task';task.mkdir(parents=True)
        (root/'a.txt').write_text('first')
        return root,task
    def test_capture_and_verify(self):
        with tempfile.TemporaryDirectory() as t:
            root,task=self.setup_scope(t)
            r=snap.capture(root,task,'baseline',['a.txt','new.txt'])
            self.assertEqual(len(r['manifest_sha256']),64)
            self.assertEqual((task/'snapshots/baseline/source/a.txt').read_text(),'first')
            m=task/'snapshots/baseline/manifest.json'
            self.assertTrue(snap.verify(root,m)['matches'])
            (root/'a.txt').write_text('second')
            self.assertFalse(snap.verify(root,m)['matches'])
    def test_no_overwrite(self):
        with tempfile.TemporaryDirectory() as t:
            root,task=self.setup_scope(t);snap.capture(root,task,'baseline',['a.txt'])
            with self.assertRaises(FileExistsError):snap.capture(root,task,'baseline',['a.txt'])
    def test_unsafe_scope(self):
        with tempfile.TemporaryDirectory() as t:
            root,task=self.setup_scope(t)
            for path in ['../private','.git/config','.agent-pr/workflow.json','C:/private','a.txt/..','/private']:
                with self.subTest(path=path),self.assertRaises(ValueError):snap.capture(root,task,'unsafe',[path])
    def test_deletion(self):
        with tempfile.TemporaryDirectory() as t:
            root,task=self.setup_scope(t)
            (root/'a.txt').unlink()
            r=snap.capture(root,task,'deleted',['a.txt'])
            self.assertIsNone(r['files'][0]['sha256'])

if __name__=='__main__':unittest.main()
