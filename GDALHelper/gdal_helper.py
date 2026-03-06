import argparse
import os
import subprocess
import sys
from typing import List

from GDALHelper.co_options import CoOptions

# ===================================================================
#  Command Base Class and Main Entry Point
# ===================================================================
COMMAND_REGISTRY = {}


# Decorator to add each function to the Command Registry
def register_command(name):
    def decorator(cls):
        COMMAND_REGISTRY[name] = cls
        return cls

    return decorator


class Command:
    """Base class for all helper commands."""

    def __init__(self, args: argparse.Namespace):
        self.args = args

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser):
        """Class-level method to add its specific arguments to the subparser."""
        raise NotImplementedError

    def execute(self):
        """Instance method to execute the command's logic."""
        raise NotImplementedError

    @staticmethod
    def print_verbose(message: str):
        """Prints a message only if the --verbose flag is set."""
        # if self.args.verbose:
        print(message, flush=True)

    @staticmethod
    def _truncate(text: str, limit: int = 400) -> str:
        """Keeps the start and end of long strings and truncates the middle."""
        if len(text) <= limit:
            return text

        # Keep first 40% and last 40% of the limit
        keep = int(limit * 0.4)
        omitted_count = len(text) - (keep * 2)

        return f"{text[:keep]} [ ... {omitted_count} chars truncated ... ] {text[-keep:]}"

    def _run_command(self, command_list: List[str]):
        """
        A helper to run an external command, capture output, and check for errors.
        Provides detailed diagnostics on failure.
        """
        import shlex  # Import locally or at top of file

        # Ensure all parts of the command are strings
        command_list = [str(item) for item in command_list]

        # Create a shell-safe string for logging (handles spaces/quotes correctly)
        # This allows you to copy-paste the log line directly into your terminal to test it.
        printable_cmd = " ".join(shlex.quote(arg) for arg in command_list)
        self.print_verbose(f"         {self._truncate(printable_cmd)}")

        # Validate command if it looks like a GDAL command
        if command_list and command_list[0].startswith("gdal"):
            try:
                validator = CoOptions()
                validator.validate(command_list)
            except ValueError as e:
                # Capture validation error and fail BEFORE running subprocess
                print("\n" + "=" * 60)
                print("⛔️ GDAL CONFIGURATION ERROR")
                print(f"   Command: {command_list[0]}")
                print(f"   Issue: {str(e)}")
                print("=" * 60 + "\n")
                raise  # Stop execution immediately

        try:
            # Run the command
            subprocess.run(
                command_list, capture_output=True, text=True, check=True
            )
        except subprocess.CalledProcessError as e:
            print("\n" + "=" * 60)
            print(f"❌ EXTERNAL COMMAND FAILED (Exit Code: {e.returncode})")
            print(f"   Command: {printable_cmd}")
            print("-" * 60)

            # Print STDOUT (often contains help text or initial processing logs)
            if e.stdout:
                print("   --- STDOUT ---")
                print(e.stdout.strip())
            else:
                print("   --- STDOUT (Empty) ---")

            print("-" * 60)

            # Print STDERR (contains the actual error message)
            if e.stderr:
                print("   --- STDERR ---")
                print(e.stderr.strip())
            else:
                print("   --- STDERR (Empty) ---")

            print("=" * 60 + "\n")

            raise  # Re-raise to stop execution


class IOCommand(Command):
    """Base class for commands that transform Input -> Output"""

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser):
        # Define the standard args here once
        parser.add_argument("input", help="Source file path")
        parser.add_argument("output", help="Destination file path")
        parser.add_argument("--overwrite", action="store_true", help="Overwrite existing output")

    def execute(self):
        # Enforce standard checks before running specific logic
        if not os.path.exists(self.args.input):
            raise FileNotFoundError(f"Input {self.args.input} missing")

        self.transform()  # Child classes implement this instead of execute()

    def transform(self):
        raise NotImplementedError


def main():
    """Parses command-line arguments and dispatches to the correct command class."""
    parser = argparse.ArgumentParser(
        description="A collection of helper utilities for GDAL-based workflows.",
        formatter_class=argparse.RawTextHelpFormatter
    )
    # Add the global --verbose flag to the main parser
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose output for all commands."
    )

    subparsers = parser.add_subparsers(
        dest="command", required=True, help="Available commands"
    )

    # Dynamically import and register the available commands
    from GDALHelper.commands import COMMANDS
    for name, command_class in COMMANDS.items():
        subparser = subparsers.add_parser(
            name, help=command_class.__doc__, formatter_class=argparse.RawTextHelpFormatter
        )
        command_class.add_arguments(subparser)
        subparser.set_defaults(handler_class=command_class)

    args = parser.parse_args()

    # Instantiate the chosen command class with the parsed args and execute it
    command_instance = args.handler_class(args)

    try:
        command_instance.execute()
    except MemoryError as e:
        print(f"❌ Error: {e}")
        sys.exit(-1)


if __name__ == "__main__":
    main()
