"""Upload the three data-provider settings from a private local dotenv file."""

import argparse
import json
import stat
import subprocess
import tempfile
from pathlib import Path

from dotenv import dotenv_values
from sync_data_providers import NAMES, PARAMETER


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--profile", default="finbot-admin")
    args = parser.parse_args()
    source = args.env_file
    info = source.stat()
    if source.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        parser.error("Use a regular dotenv file with permissions 0600.")
    settings = dotenv_values(source, interpolate=False)
    values = {name: settings.get(name) for name in NAMES}
    if any(not value or any(char in value for char in "\r\n\x00") for value in values.values()):
        parser.error("All three provider settings must have single-line values.")
    with tempfile.NamedTemporaryFile(mode="w") as payload:
        json.dump({"Name": PARAMETER, "Type": "SecureString", "Value": json.dumps(values),
                   "Tier": "Standard", "Overwrite": True}, payload)
        payload.flush()
        result = subprocess.run(
            ["aws", "--profile", args.profile, "--region", "eu-west-1", "ssm", "put-parameter",
             "--cli-input-json", "file://" + payload.name, "--query", "Version", "--output", "text",
             "--no-cli-pager"], text=True, capture_output=True, timeout=60,
        )
    version = result.stdout.strip()
    if result.returncode or not version.isdecimal():
        parser.exit(1, "AWS did not confirm the encrypted update. Check your AWS session and permissions.\n")
    print(f"Encrypted provider settings uploaded as version {version}.")


if __name__ == "__main__":
    main()
