import argparse
import subprocess
from typing import List


# ===================================================================
#  Command Base Class and Main Entry Point
# ===================================================================

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

    def print_verbose(self, message: str):
        """Prints a message only if the --verbose flag is set."""
        #if self.args.verbose:
        print(message, flush=True)

    def _run_command(self, command_list: List[str]):
        """
        A helper to run an external command, capture output, and check for errors.
        """
        # Ensure all parts of the command are strings
        command_list = [str(item) for item in command_list]
        self.print_verbose(f"   {' '.join(command_list)}")
        try:
            # capture_output=True prevents GDAL messages from cluttering the log
            subprocess.run(
                command_list, capture_output=True, text=True, check=True
            )
        except subprocess.CalledProcessError as e:
            print(f"❌ Command failed: {' '.join(e.cmd)}")
            print(f"   --- STDERR ---\n{e.stderr.strip()}")
            raise  # Re-raise the exception to halt the build step


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
    from GDALHelper.helper_commands import COMMANDS
    for name, command_class in COMMANDS.items():
        subparser = subparsers.add_parser(
            name, help=command_class.__doc__, formatter_class=argparse.RawTextHelpFormatter
        )
        command_class.add_arguments(subparser)
        subparser.set_defaults(handler_class=command_class)

    args = parser.parse_args()

    # Instantiate the chosen command class with the parsed args and execute it
    command_instance = args.handler_class(args)
    command_instance.execute()


if __name__ == "__main__":
    main()
