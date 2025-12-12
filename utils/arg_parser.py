import argparse
import logging
import os
import traceback


def validate_args(args):
    """
    Validates parsed arguments.

    Args:
        args: Parsed arguments object.
    Raises:
        ValueError: If validation fails.
    """
    # Validate --config
    if args.config:
        for config_path in args.config:
            if not os.path.exists(config_path):
                raise ValueError(f"Config file does not exist: {config_path}")

    # Validate --save_performance_results directory
    if args.save_performance_results:
        save_performance_dir = os.path.dirname(args.save_performance_results)
        if save_performance_dir and not os.path.exists(save_performance_dir):
            raise ValueError(f"The directory for saving performance results does not exist: {save_performance_dir}")


def parse_and_validate_console_args(cli_args=None):
    """
    Parses and validates console arguments.

    Args:
        cli_args: Optional CLI arguments for testing.
    Returns:
        argparse.Namespace: Parsed and validated arguments.
    Raises:
        RuntimeError: If argument parsing or validation fails.
    """
    try:
        parser = argparse.ArgumentParser(
            description="📈 Spot Grid Trading Bot - Automate your grid trading strategy with confidence\n\n"
            "This bot lets you automate your trading by implementing a grid strategy. "
            "Set your parameters, watch it execute, and manage your trades more effectively. "
            "Ideal for both beginners and experienced traders!",
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        )

        required_args = parser.add_argument_group("Required Arguments")
        required_args.add_argument(
            "--config",
            type=str,
            nargs="+",
            required=True,
            metavar="CONFIG",
            help="Path(s) to the configuration file(s) containing strategy details.",
        )

        optional_args = parser.add_argument_group("Optional Arguments")
        optional_args.add_argument(
            "--save_performance_results",
            type=str,
            metavar="FILE",
            help="Path to save simulation results (e.g., results.json).",
        )
        optional_args.add_argument(
            "--no-plot",
            action="store_true",
            help="Disable the display of plots at the end of the simulation.",
        )
        optional_args.add_argument(
            "--profile",
            action="store_true",
            help="Enable profiling for performance analysis.",
        )

        args = parser.parse_args(cli_args)
        validate_args(args)
        return args

    except SystemExit as e:
        if e.code == 0:  # Exit code 0 indicates a successful --help invocation
            raise
        logging.error(f"Argument parsing failed: {e}")
        raise RuntimeError("Failed to parse arguments. Please check your inputs.") from e

    except ValueError as e:
        logging.error(f"Validation failed: {e}")
        raise RuntimeError("Argument validation failed.") from e

    except Exception as e:
        logging.error(f"An unexpected error occurred while parsing arguments: {e}")
        logging.error(traceback.format_exc())
        raise RuntimeError("An unexpected error occurred during argument parsing.") from e
