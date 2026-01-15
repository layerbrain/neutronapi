"""
Test command for running tests with Django-like functionality.

Supports:
- Dot notation for specific tests
- --failfast to stop on first failure
- --verbosity levels (0, 1, 2, 3)
- --parallel for concurrent test execution
- --reverse to run tests in reverse order
- -k pattern matching
- --tag and --exclude-tag for test filtering
- Coverage reporting
"""
import os
import sys
import unittest
import asyncio
import fnmatch
import re
from typing import List, Optional, Tuple
from io import StringIO


class Command:
    """Test command class for running tests with Django-like options."""

    def __init__(self):
        self.help = """Run tests with Django-like functionality.

Usage:
    python manage.py test                           # Run all tests
    python manage.py test apps.brain                # Run specific app tests
    python manage.py test apps.brain.tests.test_api # Run specific module
    python manage.py test MyClass.test_method       # Run specific test

Options:
    -v, --verbosity N   Verbosity level (0=minimal, 1=normal, 2=verbose, 3=debug)
    --failfast          Stop on first failure
    --parallel N        Run tests in parallel (N workers, default=auto)
    --reverse           Run tests in reverse order
    -k PATTERN          Only run tests matching pattern
    --tag TAG           Only run tests with tag
    --exclude-tag TAG   Exclude tests with tag
    --cov, --coverage   Enable coverage reporting
    --keepdb            Keep test database between runs
    --debug-sql         Print SQL queries
    -q, --quiet         Quiet output (same as -v 0)

Examples:
    python manage.py test --failfast
    python manage.py test -k "test_create"
    python manage.py test apps.brain --parallel 4
    python manage.py test --tag slow --verbosity 2
"""
        self._pg_container = None
        self._keepdb = False

    async def safe_shutdown(self):
        """Safely shutdown database connections with timeout."""
        try:
            from neutronapi.db import shutdown_all_connections
            await asyncio.wait_for(shutdown_all_connections(), timeout=5)
        except asyncio.TimeoutError:
            pass
        except ImportError:
            pass
        except Exception:
            pass

    async def run_forced_shutdown(self):
        """Run shutdown in the current event loop context."""
        await self.safe_shutdown()

    async def _has_existing_postgres_server(self, db_config: dict) -> bool:
        """Check if PostgreSQL server is already running and accessible."""
        try:
            import asyncpg
            conn = await asyncpg.connect(
                host=db_config.get('HOST', 'localhost'),
                port=db_config.get('PORT', 5432),
                database='postgres',
                user=db_config.get('USER', 'postgres'),
                password=db_config.get('PASSWORD', ''),
            )
            await conn.close()
            return True
        except:
            return False

    async def _setup_test_database(self, db_config: dict):
        """Create a test database on existing PostgreSQL server."""
        import asyncpg
        test_db_name = f"test_{db_config.get('NAME', 'neutronapi')}"

        from neutronapi.conf import settings
        if hasattr(settings, '_settings') and 'DATABASES' in settings._settings:
            settings._settings['DATABASES']['default']['NAME'] = test_db_name

        try:
            conn = await asyncpg.connect(
                host=db_config.get('HOST', 'localhost'),
                port=db_config.get('PORT', 5432),
                database='postgres',
                user=db_config.get('USER', 'postgres'),
                password=db_config.get('PASSWORD', ''),
            )

            if not self._keepdb:
                # Clean up any dangling test databases
                dangling_dbs = await conn.fetch(
                    "SELECT datname FROM pg_database WHERE datname LIKE 'test_%'"
                )
                for db_row in dangling_dbs:
                    db_name = db_row['datname']
                    await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')

            # Check if database exists
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", test_db_name
            )
            if not exists:
                await conn.execute(f'CREATE DATABASE "{test_db_name}"')

            await conn.close()
        except Exception as e:
            print(f"Warning: Could not setup test database: {e}")

    async def _setup_test_sqlite(self, db_config: dict):
        """Setup in-memory SQLite for tests."""
        from neutronapi.conf import settings
        if hasattr(settings, '_settings') and 'DATABASES' in settings._settings:
            settings._settings['DATABASES']['default']['NAME'] = ':memory:'

    async def _cleanup_test_database(self):
        """Clean up test database if we created one."""
        if self._keepdb:
            return

        try:
            from neutronapi.conf import settings
            if hasattr(settings, '_settings') and 'DATABASES' in settings._settings:
                db_config = settings._settings['DATABASES']['default']
                db_name = db_config.get('NAME', '')
                engine = db_config.get('ENGINE', '').lower()

                if engine == 'asyncpg' and db_name.startswith('test_') and not self._pg_container:
                    import asyncpg
                    conn = await asyncpg.connect(
                        host=db_config.get('HOST', 'localhost'),
                        port=db_config.get('PORT', 5432),
                        database='postgres',
                        user=db_config.get('USER', 'postgres'),
                        password=db_config.get('PASSWORD', ''),
                    )
                    await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
                    await conn.close()
        except Exception:
            pass

    async def _run_async(self, *cmd: str, timeout: Optional[float] = None) -> Tuple[int, str, str]:
        """Run a subprocess asynchronously and capture output."""
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise
        return proc.returncode, stdout.decode(), stderr.decode()

    async def _bootstrap_postgres(self):
        """Start a disposable PostgreSQL in Docker if available."""
        import shutil

        self._pg_container = None
        host = os.getenv('PGHOST', '127.0.0.1')
        port = int(os.getenv('PGPORT', '54329'))
        dbname = os.getenv('PGDATABASE', 'temp_test')
        user = os.getenv('PGUSER', 'postgres')
        password = os.getenv('PGPASSWORD', 'postgres')

        docker = shutil.which('docker')
        if not docker:
            return False

        try:
            code, _, _ = await self._run_async(docker, 'info', timeout=5)
            if code != 0:
                return False

            image = 'postgres:15-alpine'
            code, _, _ = await self._run_async(docker, 'image', 'inspect', image, timeout=5)
            if code != 0:
                return False

            name = 'neutronapi_test_pg'
            code, out, _ = await self._run_async(docker, 'ps', '-q', '-f', f'name={name}', timeout=5)

            if not out.strip():
                code, _, err = await self._run_async(
                    docker, 'run', '-d', '--rm', '--name', name,
                    '-e', f'POSTGRES_PASSWORD={password}',
                    '-e', f'POSTGRES_DB={dbname}',
                    '-e', f'POSTGRES_USER={user}',
                    '-p', f'{port}:5432',
                    image,
                    timeout=20,
                )
                if code == 0:
                    self._pg_container = name
                else:
                    return False
            else:
                self._pg_container = name

        except (asyncio.TimeoutError, Exception):
            return False

        # Wait for PostgreSQL to be ready
        try:
            import asyncpg

            async def _wait_ready():
                for i in range(60):
                    try:
                        conn = await asyncpg.connect(
                            host=host, port=port, database=dbname, user=user, password=password
                        )
                        await conn.close()
                        return True
                    except Exception:
                        await asyncio.sleep(0.25)
                return False

            ready = await _wait_ready()
            if not ready:
                return False

        except Exception:
            return False

        try:
            from neutronapi.conf import settings
            if hasattr(settings, '_settings') and 'DATABASES' in settings._settings:
                db_config = settings._settings['DATABASES']['default']
                db_config.update({
                    'HOST': host,
                    'PORT': port,
                    'NAME': dbname,
                    'USER': user,
                    'PASSWORD': password,
                })
            return True

        except Exception:
            return False

    async def _teardown_postgres(self):
        """Stop the disposable postgres container if we started it."""
        import shutil
        if getattr(self, '_pg_container', None) and not self._keepdb:
            docker = shutil.which('docker')
            if docker:
                try:
                    await self._run_async(docker, 'stop', self._pg_container, timeout=10)
                except Exception:
                    pass

    def _filter_suite_by_pattern(self, suite: unittest.TestSuite, pattern: str) -> unittest.TestSuite:
        """Filter test suite by pattern (-k option)."""
        filtered = unittest.TestSuite()

        for test in suite:
            if isinstance(test, unittest.TestSuite):
                filtered.addTests(self._filter_suite_by_pattern(test, pattern))
            else:
                test_name = str(test)
                if fnmatch.fnmatch(test_name, f"*{pattern}*") or re.search(pattern, test_name):
                    filtered.addTest(test)

        return filtered

    def _filter_suite_by_tags(self, suite: unittest.TestSuite,
                              include_tags: List[str],
                              exclude_tags: List[str]) -> unittest.TestSuite:
        """Filter test suite by tags."""
        if not include_tags and not exclude_tags:
            return suite

        filtered = unittest.TestSuite()

        for test in suite:
            if isinstance(test, unittest.TestSuite):
                filtered.addTests(self._filter_suite_by_tags(test, include_tags, exclude_tags))
            else:
                # Get tags from test method or class
                test_method = getattr(test, test._testMethodName, None)
                test_tags = set()

                if test_method:
                    test_tags.update(getattr(test_method, 'tags', []))
                test_tags.update(getattr(test.__class__, 'tags', []))

                # Check exclusion first
                if exclude_tags and test_tags & set(exclude_tags):
                    continue

                # Check inclusion
                if include_tags:
                    if test_tags & set(include_tags):
                        filtered.addTest(test)
                else:
                    filtered.addTest(test)

        return filtered

    def _reverse_suite(self, suite: unittest.TestSuite) -> unittest.TestSuite:
        """Reverse the order of tests in suite."""
        tests = list(suite)
        tests.reverse()
        reversed_suite = unittest.TestSuite()

        for test in tests:
            if isinstance(test, unittest.TestSuite):
                reversed_suite.addTest(self._reverse_suite(test))
            else:
                reversed_suite.addTest(test)

        return reversed_suite

    async def handle(self, args: List[str]) -> int:
        """Run tests with Django-like options."""

        # Show help if requested
        if args and args[0] in ["--help", "-h", "help"]:
            print(self.help)
            return 0

        # Parse arguments
        verbosity = 1
        failfast = False
        parallel = None
        reverse = False
        pattern = None
        include_tags = []
        exclude_tags = []
        use_coverage = False
        debug_sql = False
        test_targets = []

        i = 0
        while i < len(args):
            arg = args[i]

            if arg in ("-v", "--verbosity"):
                if i + 1 < len(args):
                    try:
                        verbosity = int(args[i + 1])
                    except ValueError:
                        print(f"Error: Invalid verbosity '{args[i + 1]}'")
                        return 1
                    i += 1
            elif arg == "-v0":
                verbosity = 0
            elif arg == "-v1":
                verbosity = 1
            elif arg == "-v2":
                verbosity = 2
            elif arg == "-v3":
                verbosity = 3
            elif arg in ("-q", "--quiet"):
                verbosity = 0
            elif arg == "--failfast":
                failfast = True
            elif arg == "--parallel":
                if i + 1 < len(args) and not args[i + 1].startswith("-"):
                    try:
                        parallel = int(args[i + 1])
                    except ValueError:
                        parallel = os.cpu_count() or 4
                    i += 1
                else:
                    parallel = os.cpu_count() or 4
            elif arg == "--reverse":
                reverse = True
            elif arg == "-k":
                if i + 1 < len(args):
                    pattern = args[i + 1]
                    i += 1
            elif arg == "--tag":
                if i + 1 < len(args):
                    include_tags.append(args[i + 1])
                    i += 1
            elif arg == "--exclude-tag":
                if i + 1 < len(args):
                    exclude_tags.append(args[i + 1])
                    i += 1
            elif arg in ("--cov", "--coverage"):
                use_coverage = True
            elif arg == "--keepdb":
                self._keepdb = True
            elif arg == "--debug-sql":
                debug_sql = True
            elif not arg.startswith("-"):
                test_targets.append(arg)

            i += 1

        # Setup database
        from neutronapi.conf import settings
        if hasattr(settings, 'DATABASES'):
            db_config = settings.DATABASES.get('default', {})
            engine = db_config.get('ENGINE', '').lower()

            if engine == 'asyncpg':
                if not await self._has_existing_postgres_server(db_config):
                    if verbosity > 0:
                        print("Bootstrapping PostgreSQL container...")
                    success = await self._bootstrap_postgres()
                    if not success:
                        print("Failed to bootstrap PostgreSQL. Tests may fail.")
                        return 1
                else:
                    await self._setup_test_database(db_config)
            elif engine == 'aiosqlite':
                await self._setup_test_sqlite(db_config)

        # Apply migrations
        async def apply_project_migrations():
            try:
                base_dir = os.path.join(os.getcwd(), 'apps')
                if not os.path.isdir(base_dir):
                    return

                found_any = False
                for app_name in os.listdir(base_dir):
                    mig_dir = os.path.join(base_dir, app_name, 'migrations')
                    if os.path.isdir(mig_dir):
                        for fn in os.listdir(mig_dir):
                            if fn.endswith('.py') and fn[:3].isdigit():
                                found_any = True
                                break
                    if found_any:
                        break

                if not found_any:
                    return

                from neutronapi.db.migration_tracker import MigrationTracker
                from neutronapi.db.connection import get_databases
                tracker = MigrationTracker(base_dir='apps')
                connection = await get_databases().get_connection('default')
                await tracker.migrate(connection)
            except Exception:
                pass

        try:
            await apply_project_migrations()
        except Exception:
            pass

        # Bootstrap test models for neutronapi development
        async def bootstrap_test_models():
            try:
                if not os.path.isdir("neutronapi") or not os.path.isfile("neutronapi/__init__.py"):
                    return

                from neutronapi.db.migrations import CreateModel
                from neutronapi.db.connection import get_databases

                try:
                    from neutronapi.tests.db.test_models import TestUser
                    from neutronapi.tests.db.test_queryset import TestObject

                    test_models = [TestUser, TestObject]
                    connection = await get_databases().get_connection('default')

                    for model_cls in test_models:
                        create_operation = CreateModel(f'neutronapi.{model_cls.__name__}', model_cls._neutronapi_fields_)
                        await create_operation.database_forwards(
                            app_label='neutronapi',
                            provider=connection.provider,
                            from_state=None,
                            to_state=None,
                            connection=connection
                        )
                except ImportError:
                    pass
            except Exception:
                pass

        try:
            await bootstrap_test_models()
        except Exception:
            pass

        # Setup coverage
        cov = None
        if use_coverage or os.getenv('COVERAGE', 'false').lower() == 'true':
            try:
                import coverage
                cov = coverage.Coverage(source=["apps", "neutronapi"], branch=True)
                cov.start()
            except Exception as e:
                if verbosity > 0:
                    print(f"Warning: coverage not started: {e}")

        # Enable SQL debugging
        if debug_sql:
            import logging
            logging.getLogger('neutronapi.db').setLevel(logging.DEBUG)

        exit_code = 0
        try:
            loader = unittest.TestLoader()
            suite = unittest.TestSuite()

            def path_to_module(arg: str) -> str:
                if arg.endswith(".py"):
                    arg = arg[:-3]
                arg = arg.lstrip("./")
                return arg.replace(os.sep, ".")

            def add_target(target: str):
                # App label (directory in apps/)
                if os.path.isdir(os.path.join("apps", target, "tests")):
                    apps_dir = "apps"
                    if apps_dir not in sys.path:
                        sys.path.insert(0, apps_dir)
                    discovered = loader.discover(
                        start_dir=os.path.join("apps", target, "tests"),
                        pattern="test_*.py",
                        top_level_dir="apps"
                    )
                    suite.addTests(discovered)
                    return

                if target == "core" and os.path.isdir("core/tests"):
                    discovered = loader.discover("core/tests", pattern="test_*.py")
                    suite.addTests(discovered)
                    return

                # File system path
                if os.path.exists(target) and target.endswith(".py"):
                    module_name = path_to_module(target)
                    suite.addTests(loader.loadTestsFromName(module_name))
                    return

                # Ensure apps is in sys.path
                apps_dir = "apps"
                if os.path.isdir(apps_dir) and apps_dir not in sys.path:
                    sys.path.insert(0, apps_dir)

                # Strip apps. prefix
                if target.startswith("apps."):
                    target = target[5:]

                # Dotted path
                suite.addTests(loader.loadTestsFromName(target))

            if test_targets:
                for target in test_targets:
                    add_target(target)
            else:
                # Default: discover all tests
                test_dirs = []

                if os.path.isdir("core/tests"):
                    test_dirs.append("core/tests")

                if os.path.isdir("apps"):
                    apps_dir = "apps"
                    if apps_dir not in sys.path:
                        sys.path.insert(0, apps_dir)

                    for app_name in os.listdir("apps"):
                        app_tests_dir = os.path.join("apps", app_name, "tests")
                        if os.path.isdir(app_tests_dir):
                            test_dirs.append(app_tests_dir)

                # Also discover neutronapi internal tests when developing
                if os.path.isdir("neutronapi/tests"):
                    test_dirs.append("neutronapi/tests")

                if test_dirs:
                    for test_dir in test_dirs:
                        if test_dir.startswith("apps"):
                            discovered = loader.discover(test_dir, pattern="test_*.py", top_level_dir="apps")
                        else:
                            discovered = loader.discover(test_dir, pattern="test_*.py")
                        suite.addTests(discovered)
                else:
                    suite = loader.discover(".", pattern="test_*.py")

            # Apply filters
            if pattern:
                suite = self._filter_suite_by_pattern(suite, pattern)

            if include_tags or exclude_tags:
                suite = self._filter_suite_by_tags(suite, include_tags, exclude_tags)

            if reverse:
                suite = self._reverse_suite(suite)

            count = suite.countTestCases()
            if count == 0:
                print("No tests found.")
                return 0

            if verbosity > 0:
                print(f"Running {count} test(s)...")
                if pattern:
                    print(f"  Pattern: {pattern}")
                if include_tags:
                    print(f"  Tags: {', '.join(include_tags)}")
                if exclude_tags:
                    print(f"  Excluded tags: {', '.join(exclude_tags)}")

            # Configure output streams
            stream = sys.stderr if verbosity > 0 else StringIO()

            # Create runner with options
            runner = unittest.TextTestRunner(
                verbosity=verbosity,
                stream=stream,
                buffer=False,
                failfast=failfast
            )

            # Run tests
            if parallel and parallel > 1:
                # Parallel execution using concurrent.futures
                if verbosity > 0:
                    print(f"Running tests in parallel ({parallel} workers)...")

                import concurrent.futures

                # Flatten suite to list of tests
                all_tests = list(suite)
                results = []

                def run_single_test(test):
                    """Run a single test and return result."""
                    single_suite = unittest.TestSuite([test])
                    buffer = StringIO()
                    single_runner = unittest.TextTestRunner(
                        verbosity=0,
                        stream=buffer,
                        buffer=True,
                        failfast=False
                    )
                    result = single_runner.run(single_suite)
                    return {
                        'test': str(test),
                        'success': result.wasSuccessful(),
                        'failures': len(result.failures),
                        'errors': len(result.errors),
                        'output': buffer.getvalue()
                    }

                with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as executor:
                    future_to_test = {
                        executor.submit(run_single_test, test): test
                        for test in all_tests
                    }

                    passed = 0
                    failed = 0
                    errors = 0

                    for future in concurrent.futures.as_completed(future_to_test):
                        result = future.result()
                        if result['success']:
                            passed += 1
                            if verbosity > 1:
                                print(f"  PASS: {result['test']}")
                        else:
                            if result['errors']:
                                errors += result['errors']
                                if verbosity > 0:
                                    print(f"  ERROR: {result['test']}")
                            else:
                                failed += result['failures']
                                if verbosity > 0:
                                    print(f"  FAIL: {result['test']}")

                            if verbosity > 1:
                                print(result['output'])

                            if failfast:
                                executor.shutdown(wait=False)
                                break

                if verbosity > 0:
                    print(f"\n{passed} passed, {failed} failed, {errors} errors")

                exit_code = 0 if (failed == 0 and errors == 0) else 1
            else:
                # Sequential execution
                result = await asyncio.to_thread(runner.run, suite)

                if not result.wasSuccessful():
                    exit_code = 1
                    if verbosity > 0:
                        print(f"\n{len(result.failures)} failures, {len(result.errors)} errors")
                else:
                    if verbosity > 0:
                        print(f"\nAll {result.testsRun} tests passed!")

        except Exception as e:
            print(f"Error running tests: {e}")
            import traceback
            traceback.print_exc()
            exit_code = 1

        finally:
            # Stop coverage
            if cov is not None:
                try:
                    cov.stop()
                    cov.save()
                    if verbosity > 0:
                        print("\nCoverage report:")
                        cov.report()
                    if os.getenv('COV_HTML', 'false').lower() == 'true':
                        cov.html_report(directory='htmlcov')
                except Exception:
                    pass

            # Cleanup
            try:
                await asyncio.wait_for(self.run_forced_shutdown(), timeout=3.0)
            except (asyncio.TimeoutError, Exception):
                pass

            try:
                await asyncio.wait_for(self._cleanup_test_database(), timeout=3.0)
            except (asyncio.TimeoutError, Exception):
                pass

            try:
                await asyncio.wait_for(self._teardown_postgres(), timeout=3.0)
            except (asyncio.TimeoutError, Exception):
                pass

            # Exit behavior
            if os.getenv('NEUTRONAPI_TEST_RETURN', '0') == '1':
                return exit_code
            os._exit(exit_code)


def tag(*tags):
    """Decorator to add tags to test methods or classes.

    Usage:
        @tag('slow', 'database')
        def test_something(self):
            ...

        @tag('integration')
        class TestIntegration(unittest.TestCase):
            ...
    """
    def decorator(obj):
        obj.tags = set(getattr(obj, 'tags', set())) | set(tags)
        return obj
    return decorator
