import importlib.util,unittest,subprocess
from unittest.mock import patch
from pathlib import Path
spec=importlib.util.spec_from_file_location('verification',Path(__file__).with_name('verify-iteration.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class VerificationSafety(unittest.TestCase):
 def test_git_diff_includes_index_worktree_and_untracked(self):
  with patch.object(m.subprocess,'check_output',side_effect=[b'old.ts\0new.ts\0',b'fresh.ts\0']) as call:
   self.assertEqual(m.changed_paths('HEAD'),['fresh.ts','new.ts','old.ts'])
   self.assertEqual(call.call_args_list[0].args[0],['git','diff','--no-renames','--name-only','-z','HEAD'])
 def test_git_failure_cannot_exempt_checks(self):
  with patch.object(m,'changed_paths',side_effect=subprocess.CalledProcessError(1,'git')),patch.object(m.subprocess,'run',return_value=subprocess.CompletedProcess([],7)) as run:
   self.assertEqual(m.main([]),7);self.assertEqual(run.call_args.args[0],['npm','run','check'])
 def test_missing_runtime_cannot_exempt_checks(self):
  with patch.object(m,'changed_paths',return_value=['README.md']),patch.dict('sys.modules',{'ai_ops_kit.gates.verification_tiers':None}),patch.object(m.subprocess,'run',return_value=subprocess.CompletedProcess([],9)) as run:
   self.assertEqual(m.main([]),9);self.assertEqual(run.call_args.args[0],['npm','run','check'])
 def test_no_changes_still_require_full_checks(self):
  with patch.object(m,'changed_paths',return_value=[]),patch.object(m.subprocess,'run',return_value=subprocess.CompletedProcess([],0)) as run:
   self.assertEqual(m.main([]),0);self.assertEqual(run.call_args.args[0],['npm','run','test']);self.assertEqual(run.call_count,4)
 def test_compound_target_cannot_drop_second_runner(self):
  with patch.object(m,'changed_paths',return_value=['tests/test_a.py']),patch('ai_ops_kit.gates.verification_tiers.select_tests',return_value={'tier':'affected','full_command':False,'targeted_command':'pytest tests/test_a.py && vitest run a.test.ts'}),patch.object(m.subprocess,'run',return_value=subprocess.CompletedProcess([],0)) as run:
   self.assertEqual(m.main([]),0);self.assertEqual(run.call_args.args[0],['npm','run','test']);self.assertEqual(run.call_count,4)
if __name__=='__main__':unittest.main()
