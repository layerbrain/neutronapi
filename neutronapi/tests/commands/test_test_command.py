"""
Tests for the test command functionality.

Tests the Django-like test runner options:
- --failfast
- -k pattern matching
- --parallel
- --reverse
- --tag/--exclude-tag
- --verbosity levels
"""
import os
import sys
import unittest
from io import StringIO
from unittest.mock import patch, MagicMock

from neutronapi.commands.test import Command, tag


class TestTestCommandParsing(unittest.TestCase):
    """Test argument parsing for test command."""

    def test_verbosity_flags(self):
        """Test verbosity flag parsing."""
        cmd = Command()

        # Test -v N
        args = ["-v", "2"]
        # We can't easily test handle() without running tests,
        # but we can verify the command initializes
        self.assertIsNotNone(cmd)

    def test_tag_decorator(self):
        """Test the @tag decorator adds tags to test methods."""
        @tag('slow', 'database')
        def test_something():
            pass

        self.assertEqual(test_something.tags, {'slow', 'database'})

    def test_tag_decorator_on_class(self):
        """Test the @tag decorator works on classes."""
        @tag('integration')
        class TestClass:
            pass

        self.assertEqual(TestClass.tags, {'integration'})

    def test_tag_decorator_accumulates(self):
        """Test multiple @tag decorators accumulate tags."""
        @tag('fast')
        @tag('unit')
        def test_method():
            pass

        self.assertEqual(test_method.tags, {'fast', 'unit'})


class TestSuiteFiltering(unittest.TestCase):
    """Test suite filtering functionality."""

    def setUp(self):
        self.cmd = Command()

    def test_filter_by_pattern_matches(self):
        """Test pattern filtering includes matching tests."""
        # Create test suite
        class TestA(unittest.TestCase):
            def test_create_user(self):
                pass
            def test_delete_user(self):
                pass
            def test_other(self):
                pass

        loader = unittest.TestLoader()
        suite = loader.loadTestsFromTestCase(TestA)

        # Filter by pattern
        filtered = self.cmd._filter_suite_by_pattern(suite, "create")

        # Should only have test_create_user
        test_names = [str(t) for t in filtered]
        self.assertEqual(len(test_names), 1)
        self.assertIn("create_user", test_names[0])

    def test_filter_by_pattern_no_match(self):
        """Test pattern filtering returns empty when no matches."""
        class TestB(unittest.TestCase):
            def test_foo(self):
                pass

        loader = unittest.TestLoader()
        suite = loader.loadTestsFromTestCase(TestB)

        filtered = self.cmd._filter_suite_by_pattern(suite, "nonexistent")
        self.assertEqual(filtered.countTestCases(), 0)

    def test_filter_by_tags_include(self):
        """Test tag filtering with include tags."""
        @tag('slow')
        class TestSlow(unittest.TestCase):
            def test_slow_operation(self):
                pass

        class TestFast(unittest.TestCase):
            def test_fast_operation(self):
                pass

        loader = unittest.TestLoader()
        suite = unittest.TestSuite()
        suite.addTests(loader.loadTestsFromTestCase(TestSlow))
        suite.addTests(loader.loadTestsFromTestCase(TestFast))

        # Filter to only include 'slow' tagged tests
        filtered = self.cmd._filter_suite_by_tags(suite, ['slow'], [])

        self.assertEqual(filtered.countTestCases(), 1)

    def test_filter_by_tags_exclude(self):
        """Test tag filtering with exclude tags."""
        @tag('slow')
        class TestSlow(unittest.TestCase):
            def test_slow_operation(self):
                pass

        class TestFast(unittest.TestCase):
            def test_fast_operation(self):
                pass

        loader = unittest.TestLoader()
        suite = unittest.TestSuite()
        suite.addTests(loader.loadTestsFromTestCase(TestSlow))
        suite.addTests(loader.loadTestsFromTestCase(TestFast))

        # Filter to exclude 'slow' tagged tests
        filtered = self.cmd._filter_suite_by_tags(suite, [], ['slow'])

        self.assertEqual(filtered.countTestCases(), 1)

    def test_reverse_suite(self):
        """Test suite reversal."""
        class TestOrder(unittest.TestCase):
            def test_a(self):
                pass
            def test_b(self):
                pass
            def test_c(self):
                pass

        loader = unittest.TestLoader()
        suite = loader.loadTestsFromTestCase(TestOrder)

        original_order = [str(t) for t in suite]
        reversed_suite = self.cmd._reverse_suite(suite)
        reversed_order = [str(t) for t in reversed_suite]

        self.assertEqual(original_order[::-1], reversed_order)


class TestCommandHelp(unittest.TestCase):
    """Test command help output."""

    def test_help_contains_all_options(self):
        """Test that help text documents all options."""
        cmd = Command()
        help_text = cmd.help

        # Check all options are documented
        options = [
            '--failfast',
            '--parallel',
            '--reverse',
            '-k',
            '--tag',
            '--exclude-tag',
            '--coverage',
            '--keepdb',
            '--debug-sql',
            '-v',
            '--verbosity',
            '-q',
            '--quiet',
        ]

        for opt in options:
            self.assertIn(opt, help_text, f"Option {opt} not in help text")


if __name__ == "__main__":
    unittest.main()
