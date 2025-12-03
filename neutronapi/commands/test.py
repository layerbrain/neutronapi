"""
Test command for running database tests.
Runs database tests with dot notation support for specific tests.
"""
import os
import sys
import unittest
import asyncio
from typing import List, Optional, Tuple


class Command:
    """Test command class for running database tests."""

    def __init__(self):
        self.help = "Run database tests with dot notation support"
        self._pg_container = None

    async def safe_shutdown(self):
        """Safely shutdown database connections with timeout."""
        try:
            from neutronapi.db import shutdown_all_connections
            await asyncio.wait_for(shutdown_all_connections(), timeout=5)
        except asyncio.TimeoutError:
            print("Warning: Database shutdown timed out, forcing shutdown.")
        except ImportError:
            # No database connections to shut down
            pass
        except Exception as e:
            print(f"Warning: Exception during database shutdown: {e}")

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
                database='postgres',  # Connect to default postgres DB to test connection
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
        
        # Update settings to use test database
        from neutronapi.conf import settings
        if hasattr(settings, '_settings') and 'DATABASES' in settings._settings:
            settings._settings['DATABASES']['default']['NAME'] = test_db_name
            
        try:
            # Connect to postgres db to manage test databases
            conn = await asyncpg.connect(
                host=db_config.get('HOST', 'localhost'),
                port=db_config.get('PORT', 5432),
                database='postgres',
                user=db_config.get('USER', 'postgres'),
                password=db_config.get('PASSWORD', ''),
            )
            
            # Clean up any dangling test databases from previous runs
            print("Cleaning up any dangling test databases...")
            dangling_dbs = await conn.fetch(
                "SELECT datname FROM pg_database WHERE datname LIKE 'test_%'"
            )
            for db_row in dangling_dbs:
                db_name = db_row['datname']
                print(f"Dropping dangling test database: {db_name}")
                await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
            
            # Create our test database
            await conn.execute(f'CREATE DATABASE "{test_db_name}"')
            await conn.close()
            
            print(f"Created clean test database: {test_db_name}")
        except Exception as e:
            print(f"Warning: Could not create test database: {e}")

    async def _setup_test_sqlite(self, db_config: dict):
        """Setup in-memory SQLite for tests."""
        from neutronapi.conf import settings
        if hasattr(settings, '_settings') and 'DATABASES' in settings._settings:
            settings._settings['DATABASES']['default']['NAME'] = ':memory:'
            print("Using in-memory SQLite for tests")

    async def _cleanup_test_database(self):
        """Clean up test database if we created one."""
        try:
            from neutronapi.conf import settings
            if hasattr(settings, '_settings') and 'DATABASES' in settings._settings:
                db_config = settings._settings['DATABASES']['default']
                db_name = db_config.get('NAME', '')
                engine = db_config.get('ENGINE', '').lower()
                
                if engine == 'asyncpg' and db_name.startswith('test_') and not self._pg_container:
                    # Only cleanup if we created a test database on existing server
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
                    print(f"Cleaned up test database: {db_name}")
        except Exception as e:
            print(f"Warning: Could not cleanup test database: {e}")

    async def _run_async(self, *cmd: str, timeout: Optional[float] = None) -> Tuple[int, str, str]:
        """Run a subprocess asynchronously and capture output."""
        import asyncio
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
        # Start a disposable PostgreSQL in Docker if available (async)
        import shutil

        self._pg_container = None
        host = os.getenv('PGHOST', '127.0.0.1')
        port = int(os.getenv('PGPORT', '54329'))  # non-standard to avoid clashes
        dbname = os.getenv('PGDATABASE', 'temp_test')
        user = os.getenv('PGUSER', 'postgres')
        password = os.getenv('PGPASSWORD', 'postgres')

        # Check if Docker is available
        docker = shutil.which('docker')
        if not docker:
            print('Docker not found, cannot bootstrap PostgreSQL')
            return False

        try:
            # Check if docker daemon is running
            code, _, _ = await self._run_async(docker, 'info', timeout=5)
            if code != 0:
                print('Docker daemon not running, cannot bootstrap PostgreSQL')
                return False

            # Ensure the required image exists locally to avoid a network pull
            image = 'postgres:15-alpine'
            code, _, _ = await self._run_async(docker, 'image', 'inspect', image, timeout=5)
            if code != 0:
                print(f"Docker image '{image}' not present locally; skipping PostgreSQL bootstrap (no network pulls)")
                return False

            # Check if a container with our name exists; if not, run one
            name = 'neutronapi_test_pg'
            code, out, _ = await self._run_async(docker, 'ps', '-q', '-f', f'name={name}', timeout=5)

            if not out.strip():
                print(f'Starting PostgreSQL container on port {port}...')
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
                    print(f'PostgreSQL container started: {name}')
                else:
                    print(f'Failed to start PostgreSQL container: {err.strip()}')
                    return False
            else:
                self._pg_container = name
                print(f'Using existing PostgreSQL container: {name}')

        except asyncio.TimeoutError:
            print('Docker command timed out during PostgreSQL bootstrap')
            return False
        except Exception as e:
            print(f"Error with Docker: {e}")
            return False

        # Wait for PostgreSQL to be ready
        print('Waiting for PostgreSQL to be ready...')
        try:
            import asyncpg

            async def _wait_ready():
                for i in range(60):  # Wait up to 15 seconds
                    try:
                        conn = await asyncpg.connect(
                            host=host, port=port, database=dbname, user=user, password=password
                        )
                        await conn.close()
                        return True
                    except Exception:
                        if i % 4 == 0:  # Print every second
                            print(f'  Waiting for PostgreSQL... ({i // 4 + 1}s)')
                        await asyncio.sleep(0.25)
                return False

            ready = await _wait_ready()
            if not ready:
                print('PostgreSQL failed to become ready in time')
                return False

        except Exception as e:
            print(f"Error waiting for PostgreSQL: {e}")
            return False

        # Update settings with container connection info
        try:
            from neutronapi.conf import settings
            if hasattr(settings, '_settings') and 'DATABASES' in settings._settings:
                # Update the existing database config with container details
                db_config = settings._settings['DATABASES']['default']
                db_config.update({
                    'HOST': host,
                    'PORT': port,
                    'NAME': dbname,
                    'USER': user,
                    'PASSWORD': password,
                })
                print(f'Updated database config: {db_config}')
            else:
                print("Warning: Could not update database settings")

            print(f'✓ PostgreSQL ready at {host}:{port}/{dbname}')
            return True

        except Exception as e:
            print(f"Error configuring PostgreSQL: {e}")
            return False

    async def _teardown_postgres(self):
        # Stop the disposable postgres container if we started it
        import shutil
        if getattr(self, '_pg_container', None):
            docker = shutil.which('docker')
            if docker:
                try:
                    await self._run_async(docker, 'stop', self._pg_container, timeout=10)
                except Exception:
                    pass

    async def handle(self, args: List[str]) -> int:
        """
        Run database tests with dot notation support.

        Usage:
            python manage.py test                    # Run all tests
            python manage.py test core.tests        # Run specific module tests
            python manage.py test core.tests.db.test_db.TestModel.test_creation  # Specific test
            python manage.py test --help            # Show help

        Examples:
            python manage.py test
            python manage.py test core.tests.db.test_db
            python manage.py test core.tests.db.test_db.TestModel.test_creation
        """

        # Show help if requested
        if args and args[0] in ["--help", "-h", "help"]:
            print(f"{self.help}\n")
            print(self.handle.__doc__)
            return

        # Check database configuration and setup test database
        from neutronapi.conf import settings
        if hasattr(settings, 'DATABASES'):
            db_config = settings.DATABASES.get('default', {})
            engine = db_config.get('ENGINE', '').lower()
            
            if engine == 'asyncpg':
                # For PostgreSQL, check if user has existing server or use Docker
                if not await self._has_existing_postgres_server(db_config):
                    print("No existing PostgreSQL server found, bootstrapping test database container...")
                    success = await self._bootstrap_postgres()
                    if not success:
                        print("Failed to bootstrap PostgreSQL container. Tests may fail.")
                        return 1
                else:
                    print("Using existing PostgreSQL server for tests...")
                    await self._setup_test_database(db_config)
            elif engine == 'aiosqlite':
                # For SQLite, always use in-memory test database
                await self._setup_test_sqlite(db_config)
        else:
            print("Warning: No DATABASES configuration found")

        # Apply project migrations (if any) using the file-based tracker
        async def apply_project_migrations():
            try:
                base_dir = os.path.join(os.getcwd(), 'apps')
                if not os.path.isdir(base_dir):
                    return
                # Quick check: any migrations directory with numbered files?
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
                print('✓ Applied project migrations for tests')
            except Exception as e:
                print(f"Warning: Failed to apply project migrations: {e}")

        try:
            await apply_project_migrations()
        except Exception:
            pass

        # Bootstrap internal test models (only when developing neutronapi itself)
        async def bootstrap_test_models():
            try:
                # Only bootstrap test models if we're in the neutronapi development environment
                # This is indicated by the presence of neutronapi source code in the current directory
                if not os.path.isdir("neutronapi") or not os.path.isfile("neutronapi/__init__.py"):
                    return  # We're not in the neutronapi development environment
                
                from neutronapi.db.migrations import CreateModel
                from neutronapi.db.connection import get_databases
                
                # Try to discover test models from local neutronapi.tests.db
                try:
                    from neutronapi.tests.db.test_models import TestUser
                    from neutronapi.tests.db.test_queryset import TestObject
                    
                    test_models = [TestUser, TestObject]
                    
                    # Create tables for test models using migrations
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
                    
                    print(f"✓ Bootstrapped {len(test_models)} internal test models")
                    
                except ImportError:
                    # Silently skip if test models not available
                    pass
                except Exception as e:
                    print(f"Warning: Failed to bootstrap test models: {e}")
                    
            except Exception as e:
                print(f"Warning: Could not bootstrap test models: {e}")
        
        # Run the bootstrap
        try:
            await bootstrap_test_models()
        except Exception as e:
            print(f"Error during test model bootstrap: {e}")

        print("Running tests...")

        exit_code = 0
        try:
            loader = unittest.TestLoader()
            # Basic flags handling: -q quiet, -v verbose
            verbosity = 2
            filtered_args: List[str] = []
            use_coverage = False
            cov = None
            for a in args:
                if a in ("-q", "--quiet"):
                    verbosity = 1
                elif a in ("-v", "--verbose"):
                    verbosity = 2
                elif a in ("--cov", "--coverage"):
                    use_coverage = True
                else:
                    filtered_args.append(a)

            # Force unbuffered output for real-time test results
            sys.stdout.reconfigure(line_buffering=True)
            sys.stderr.reconfigure(line_buffering=True)
            runner = unittest.TextTestRunner(verbosity=verbosity, stream=sys.stderr, buffer=False)
            suite = unittest.TestSuite()

            # Start coverage if requested or env flag set
            if use_coverage or os.getenv('COVERAGE', 'false').lower() == 'true':
                try:
                    import coverage
                    cov = coverage.Coverage(source=["core"], branch=True)
                    cov.start()
                except Exception as e:
                    print(f"Warning: coverage not started: {e}")

            def path_to_module(arg: str) -> str:
                # Convert a filesystem path to dotted module path
                if arg.endswith(".py"):
                    arg = arg[:-3]
                arg = arg.lstrip("./")
                return arg.replace(os.sep, ".")

            def add_target(target: str):
                # If target is an app label (directory in apps/ or 'core')
                if os.path.isdir(os.path.join("apps", target, "tests")):
                    # Add the apps directory to sys.path so nested test modules can be imported
                    apps_dir = "apps"
                    if apps_dir not in sys.path:
                        sys.path.insert(0, apps_dir)
                    # Start discovery from apps directory with proper top_level_dir
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

                # If it's a file system path
                if os.path.exists(target) and target.endswith(".py"):
                    module_name = path_to_module(target)
                    suite.addTests(loader.loadTestsFromName(module_name))
                    return

                # Ensure apps is in sys.path for dotted path imports
                apps_dir = "apps"
                if os.path.isdir(apps_dir) and apps_dir not in sys.path:
                    sys.path.insert(0, apps_dir)

                # Strip apps. prefix if present since apps is in sys.path
                if target.startswith("apps."):
                    target = target[5:]

                # Otherwise, treat as dotted path (module, class, or method)
                suite.addTests(loader.loadTestsFromName(target))

            if filtered_args:
                for target in filtered_args:
                    add_target(target)
            else:
                # Default: discover all apps/*/tests (project-specific only)
                test_dirs = []

                # Only look for project tests, not installed package tests
                # Support legacy core/tests if it exists in the current project
                if os.path.isdir("core/tests"):
                    test_dirs.append("core/tests")

                if os.path.isdir("apps"):
                    # Add apps to sys.path for proper module resolution
                    apps_dir = "apps"
                    if apps_dir not in sys.path:
                        sys.path.insert(0, apps_dir)
                    
                    for app_name in os.listdir("apps"):
                        app_tests_dir = os.path.join("apps", app_name, "tests")
                        if os.path.isdir(app_tests_dir):
                            test_dirs.append(app_tests_dir)

                if test_dirs:
                    for test_dir in test_dirs:
                        # Use proper top_level_dir for apps
                        if test_dir.startswith("apps"):
                            discovered = loader.discover(test_dir, pattern="test_*.py", top_level_dir="apps")
                        else:
                            discovered = loader.discover(test_dir, pattern="test_*.py")
                        suite.addTests(discovered)
                else:
                    suite = loader.discover(".", pattern="test_*.py")

            count = suite.countTestCases()
            if count == 0:
                print("No tests found.")
                return

            print(f"Running {count} test(s)...")
            # Run unittest in a worker thread to avoid event-loop conflicts
            result = await asyncio.to_thread(runner.run, suite)

            if not result.wasSuccessful():
                print(f"\nTests failed: {len(result.failures)} failures, {len(result.errors)} errors")
                exit_code = 1
            else:
                print(f"\nAll {result.testsRun} tests passed!")

        except Exception as e:
            print(f"Error running tests: {e}")
            import traceback
            traceback.print_exc()
            exit_code = 1
        finally:
            print("Closing test environments...")
            # Stop and report coverage if active
            try:
                if cov is not None:
                    cov.stop()
                    cov.save()
                    cov.report()
                    if os.getenv('COV_HTML', 'false').lower() == 'true':
                        cov.html_report(directory='htmlcov')
            except Exception as e:
                print(f"Warning: coverage reporting failed: {e}")
            
            # Cleanup with timeout
            try:
                await asyncio.wait_for(self.run_forced_shutdown(), timeout=3.0)
            except asyncio.TimeoutError:
                print("Warning: Database cleanup timed out")
            except Exception as e:
                print(f"Warning: Database cleanup failed: {e}")
            
            # Cleanup test database if needed
            try:
                await asyncio.wait_for(self._cleanup_test_database(), timeout=3.0)
            except asyncio.TimeoutError:
                print("Warning: Test database cleanup timed out")
            except Exception as e:
                print(f"Warning: Test database cleanup failed: {e}")
            
            # Best-effort: stop ephemeral postgres if we started it
            try:
                await asyncio.wait_for(self._teardown_postgres(), timeout=3.0)
            except asyncio.TimeoutError:
                print("Warning: PostgreSQL cleanup timed out")
            except Exception:
                pass
                
            # Default: hard-exit to ensure full shutdown (no lingering loops/threads)
            # For programmatic callers/tests, set NEUTRONAPI_TEST_RETURN=1 to receive the code instead.
            if os.getenv('NEUTRONAPI_TEST_RETURN', '0') == '1':
                return exit_code
            os._exit(exit_code)
