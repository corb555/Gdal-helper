import sys
import traceback

from GDALHelper.commands import build_parser
from GDALHelper.utils import ApplicationError


# ===================================================================
# Main Entry Point
# ===================================================================

def main() -> None:
    """Parse command-line arguments and dispatch the selected command."""
    parser = build_parser(
        description="A collection of helper utilities for GDAL-based workflows.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose output for all commands.",
    )

    args = parser.parse_args()
    command_instance = args.command_class(args)

    try:
        command_instance.execute()

    except ApplicationError as exc:
        print(f"❌ Error: {exc}")
        sys.exit(1)

    except KeyboardInterrupt:
        print("\n⚠️ Command cancelled.")
        sys.exit(130)

    except Exception as exc:
        print(f"❌ Unexpected programming error: {exc}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
