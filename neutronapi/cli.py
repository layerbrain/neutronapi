"""
NeutronAPI CLI entrypoint.

Exposes a command interface similar to `manage.py`, so installed users
can run `neutronapi <command>` in their projects.

Discovers built-in commands from `neutronapi.commands` and project
commands from `apps/*/commands`.
"""
from __future__ import annotations

import os
import sys
import asyncio
import importlib
from typing import Dict, Any


def _project_required_files() -> list[str]:
    base = os.getcwd()
    apps_dir = os.path.join(base, 'apps')
    return [
        os.path.join(apps_dir, 'settings.py'),
        os.path.join(apps_dir, 'entry.py'),
    ]


def _discover_commands_from(prefix: str, exclude_cli_only: bool = False) -> Dict[str, Any]:
    commands: Dict[str, Any] = {}
    cli_only_commands = {'startproject'}  # Commands only available via CLI, not manage.py

    try:
        import pkgutil
        pkg = importlib.import_module(f"{prefix}.commands")
        for _, name, ispkg in pkgutil.iter_modules(pkg.__path__):
            if not ispkg:
                # Skip CLI-only commands if requested
                if exclude_cli_only and name in cli_only_commands:
                    continue

                try:
                    module = importlib.import_module(f"{prefix}.commands.{name}")
                    if hasattr(module, 'Command'):
                        commands[name] = module.Command()
                except Exception:
                    # Skip modules that fail to import (syntax errors, missing deps, etc.)
                    pass
    except Exception:
        pass
    return commands


def discover_commands() -> Dict[str, Any]:
    """Discover commands from neutronapi.commands and apps/*/commands directories."""
    commands: Dict[str, Any] = {}

    # Built-in commands
    try:
        commands.update(_discover_commands_from("neutronapi"))
    except Exception:
        pass

    # Project app-specific commands
    apps_dir = os.path.join(os.getcwd(), 'apps')
    if os.path.isdir(apps_dir):
        # Add the project root to sys.path so we can import apps
        project_root = os.getcwd()
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        
        for app_name in os.listdir(apps_dir):
            app_path = os.path.join(apps_dir, app_name)
            if os.path.isdir(app_path) and not app_name.startswith('.'):
                app_commands_dir = os.path.join(app_path, 'commands')
                if os.path.isdir(app_commands_dir):
                    for filename in os.listdir(app_commands_dir):
                        if filename.endswith('.py') and not filename.startswith('__'):
                            command_name = filename[:-3]
                            try:
                                # Clear import cache to ensure fresh imports during testing
                                module_name = f"apps.{app_name}.commands.{command_name}"
                                parent_modules = [
                                    "apps",
                                    f"apps.{app_name}",
                                    f"apps.{app_name}.commands",
                                    module_name
                                ]
                                for mod in parent_modules:
                                    if mod in sys.modules:
                                        del sys.modules[mod]
                                
                                module = importlib.import_module(module_name)
                                if hasattr(module, 'Command'):
                                    commands[command_name] = module.Command()
                            except Exception as e:
                                # Skip modules that fail to import (syntax errors, missing deps, etc.)
                                # print(f"Failed to import apps.{app_name}.commands.{command_name}: {e}")
                                pass

    return commands


def main() -> None:
    # Load commands first so startproject can run outside a project
    commands = discover_commands()

    # Add CLI-only commands (not available in manage.py)
    from neutronapi.commands import startproject
    commands['startproject'] = startproject.Command()

    if len(sys.argv) < 2:
        print("Available commands:")
        for cmd in sorted(commands.keys()):
            print(f"  {cmd}")
        print("\nUse 'neutronapi <command> --help' for detailed usage")
        return

    command_name = sys.argv[1]
    args = sys.argv[2:]

    # Handle --help for any command
    if "--help" in args or command_name == "--help":
        if command_name == "--help":
            print("Available commands:")
            for cmd in sorted(commands.keys()):
                command_obj = commands[cmd]
                help_text = getattr(command_obj, 'help', 'No description available')
                print(f"  {cmd:<15} {help_text}")
            print("\nUse 'neutronapi <command> --help' for detailed usage")
            return
        elif command_name in commands:
            # Show help for specific command
            command_obj = commands[command_name]
            help_text = getattr(command_obj, 'help', 'No description available')
            print(f"Usage: neutronapi {command_name}")
            print(f"Description: {help_text}")

            # Show additional help if command has detailed help
            if hasattr(command_obj, 'get_help'):
                print(command_obj.get_help())
            return

    if command_name not in commands:
        print(f"Unknown command: {command_name}")
        print("Available commands:", ", ".join(sorted(commands.keys())))
        sys.exit(1)

    # Validate project layout only for project-scoped commands
    # Keep 'test' and other generic commands runnable without a project scaffold
    requires_project = {"migrate", "makemigrations", "startapp", "shell"}
    if command_name in requires_project:
        missing = [p for p in _project_required_files() if not os.path.isfile(p)]
        if missing:
            rel_missing = [os.path.relpath(p, os.getcwd()) for p in missing]
            print("Project misconfigured: required files missing.")
            for p in rel_missing:
                print(f"  - {p}")
            print("Both 'apps/entry.py' and 'apps/settings.py' must exist at the same level.")
            sys.exit(1)

    async def _dispatch():
        databases = None
        try:
            # Initialize database connections automatically
            from neutronapi.db import get_databases
            databases = get_databases()

            command = commands[command_name]
            handle = getattr(command, 'handle', None)
            if handle is None:
                raise RuntimeError("Command has no handle()")

            # ALWAYS expect async - no fallback!
            if not asyncio.iscoroutinefunction(handle):
                raise RuntimeError(f"Command {command_name} handle() must be async")

            result = await handle(args)
            exit_code = result if isinstance(result, int) else 0
        except KeyboardInterrupt:
            print("\nOperation cancelled by user.")
            exit_code = 1
        except SystemExit as e:
            exit_code = e.code if e.code is not None else 1
            raise
        except Exception as e:
            print(f"\nAn error occurred while running command '{command_name}': {e}")
            if os.getenv("DEBUG", "False").lower() == "true":
                import traceback
                traceback.print_exc()
            exit_code = 1
        finally:
            # Clean up database connections automatically (same pattern as test command)
            try:
                from neutronapi.db import shutdown_all_connections
                await asyncio.wait_for(shutdown_all_connections(), timeout=5)
            except asyncio.TimeoutError:
                print("Warning: Database shutdown timed out, forcing shutdown.")
            except ImportError:
                # No database connections to shut down
                pass
            except Exception:
                pass

            # Flush output buffers before force exit
            sys.stdout.flush()
            sys.stderr.flush()

            # Force exit like test command does (same pattern as test command)
            os._exit(exit_code)

    asyncio.run(_dispatch())


if __name__ == "__main__":
    main()
