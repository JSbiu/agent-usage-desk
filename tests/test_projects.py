"""Working-directory normalization used to group usage by project."""
import unittest

from codex_monitor.core import project_key


class ProjectKeyTests(unittest.TestCase):
    def test_windows_path_uses_forward_slashes(self):
        self.assertEqual(project_key("C:\\Users\\a\\proj\\"), "C:/Users/a/proj")

    def test_drive_letter_is_uppercased(self):
        self.assertEqual(project_key("d:/x/y"), "D:/x/y")

    def test_trailing_separators_are_removed(self):
        self.assertEqual(project_key("/home/a/proj///"), "/home/a/proj")

    def test_unc_prefix_is_preserved(self):
        self.assertEqual(
            project_key("\\\\wsl$\\Ubuntu-22.04\\home\\jason\\projects\\x"),
            "//wsl$/Ubuntu-22.04/home/jason/projects/x")

    def test_unusable_values_yield_empty_key(self):
        for value in (None, "", "   ", 123, ["a"], "a\nb", "a\x00b"):
            self.assertEqual(project_key(value), "", repr(value))

    def test_overlong_path_is_rejected(self):
        self.assertEqual(project_key("C:/" + "x" * 600), "")


if __name__ == "__main__":
    unittest.main()
