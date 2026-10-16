import importlib.util
import sys
from pathlib import Path


MODULE = Path(__file__).parents[1] / "safemin.py"
spec = importlib.util.spec_from_file_location("safemin_test_module", MODULE)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_groups_multiple_physical_hunks_in_one_function(tmp_path):
    task = {"patch": [{"filename": "pkg/a.py", "status": "modified", "patch": """@@ -2,3 +2,4 @@\n def run():\n     value = 1\n+    value += 1\n@@ -8,3 +9,4 @@\n def run():\n     return value\n+\n"""}]}
    hunks = module.extract_hunks(task, tmp_path, {"pkg/a.py": "def run():\n    value = 1\n    return value\n"})
    assert len(hunks) == 1
    assert hunks[0].symbol == "run"
    assert len(hunks[0].patch_sections) == 2


def test_import_only_hunks_are_marked(tmp_path):
    task = {"patch": [{"filename": "pkg/a.py", "status": "modified", "patch": """@@ -1,1 +1,2 @@\n+import os\n def run():\n"""}]}
    hunks = module.extract_hunks(task, tmp_path)
    assert len(hunks) == 1
    assert hunks[0].import_only is True
