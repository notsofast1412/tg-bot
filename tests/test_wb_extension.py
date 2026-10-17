from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which('node') is None, reason='Optional JS harness requires Node; no browser or network')
def test_extension_job_navigation_capture_and_secret_filtering():
    script = Path(__file__).with_name('wb_extension_test.cjs')
    result = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
