# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# dev_pipeline.py — Dev pipeline orchestration.
# Runs all pre-commit/pre-deploy steps in one call.
#
# Usage: python dev_pipeline.py [--manifest] [--all]

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))


def run_manifest():
    """Generate the complete project manifest."""
    from manifest_generator import ManifestGenerator
    gen = ManifestGenerator()
    gen.generate()
    path = gen.save()
    print(f"Manifest: {gen.file_count} files, {gen.total_entries} total entries -> {path}")


def main():
    parser = argparse.ArgumentParser(description="ChatHealthy.ai Dev Pipeline")
    parser.add_argument("--manifest", action="store_true", help="Regenerate project manifest")
    parser.add_argument("--all", action="store_true", help="Run all pipeline steps")
    args = parser.parse_args()

    if args.all or args.manifest:
        print("=== Manifest ===")
        run_manifest()

    if not any(vars(args).values()):
        parser.print_help()


if __name__ == "__main__":
    main()
