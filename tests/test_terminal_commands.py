"""Guard against the exact interactive-shell hazards previously observed."""
from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from check_terminal_commands import scanMarkdown  # noqa: E402


class TerminalCommandTests(unittest.TestCase):
    def checkFlag(self, command: str, rule: str):
        findings = scanMarkdown(f"```bash\n{command}\n```", "user_commands.md")
        self.assertIn(rule, {finding.rule for finding in findings}, command)

    def testSafeStandaloneCommands(self):
        sample = """```bash
cd ~/blackswan
python3 -m unittest discover -s tests -v
git status --short
```
"""
        self.assertEqual(scanMarkdown(sample), [])

    def testSetErrexit(self):
        self.checkFlag("set -e", "shell-options")

    def testSetEuo(self):
        self.checkFlag("set -euo pipefail", "shell-options")

    def testSetOtherShellState(self):
        self.checkFlag("set -o pipefail", "shell-options")
        self.checkFlag("set -u", "shell-options")

    def testExitInErrorHandler(self):
        self.checkFlag("test -f config || { echo missing; exit 1; }", "shell-exit")

    def testExplicitExit(self):
        self.checkFlag("$ exit 1", "shell-exit")

    def testExec(self):
        self.checkFlag("exec ./runTrading", "shell-exec")

    def testTrap(self):
        self.checkFlag("trap 'echo failed' EXIT", "shell-trap")

    def testSource(self):
        self.checkFlag("source ~/.bashrc", "source-current-shell")

    def testBashErrexit(self):
        self.checkFlag("bash -e ./install.sh", "shell-with-errexit")

    def testBroadKill(self):
        self.checkFlag("pkill -f blackswan", "broad-process-kill")
        self.checkFlag("killall python", "broad-process-kill")

    def testDestruction(self):
        self.checkFlag("rm -rf ~/blackswan/data", "recursive-delete")
        self.checkFlag("git reset --hard HEAD", "force-git-discard")

    def testRemoteShell(self):
        self.checkFlag("curl https://example.org/install | bash", "pipe-to-shell")

    def testIgnoreProseAndNonShellCode(self):
        sample = """Never use `set -e`.
```python
print('set -e')
```
```bash
# set -e would be unsafe
printf '%s\\n' safe
```
"""
        self.assertEqual(scanMarkdown(sample), [])

    def testLineNumberAndFilename(self):
        result = scanMarkdown("text\n```sh\nexit 1\n```\n", "file.md")
        self.assertEqual((result[0].filename, result[0].lineNumber), ("file.md", 3))


if __name__ == "__main__":
    unittest.main()
