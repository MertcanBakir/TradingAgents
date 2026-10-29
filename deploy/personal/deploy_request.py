"""Send the restricted SSM document and wait for this exact instance's result."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path


def aws(*args):
    result = subprocess.run(["aws", *args, "--region", "eu-west-1", "--output", "json"],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        # GetCommandInvocation is eventually consistent immediately after send.
        if "InvocationDoesNotExist" in result.stderr:
            return None
        raise RuntimeError("AWS command failed; private output suppressed")
    return json.loads(result.stdout)


def parameters(digest, commit, script_hash):
    for value, pattern in ((digest, r"sha256:[0-9a-f]{64}"), (commit, r"[0-9a-f]{40}"), (script_hash, r"[0-9a-f]{64}")):
        if not re.fullmatch(pattern, value):
            raise ValueError("Invalid deployment parameter")
    return {"ImageDigest": [digest], "CommitSha": [commit], "ScriptSha256": [script_hash]}


def wait(command_id, *, invoke=aws, clock=time.monotonic, sleep=time.sleep):
    until = clock() + 1000
    while clock() < until:
        response = invoke("ssm", "get-command-invocation", "--command-id", command_id,
                          "--instance-id", "i-0cc2d47020ed7347c")
        if response and response["Status"] not in {"Pending", "InProgress", "Delayed"}:
            if response["Status"] != "Success" or response["ResponseCode"] != 0:
                raise RuntimeError("Remote deployment failed; inspect the restricted SSM command result")
            result = json.loads(response["StandardOutputContent"].strip())
            if result.get("status") not in {"deployed", "already_current"}:
                raise RuntimeError("Remote helper did not confirm a verified deployment")
            print(json.dumps(result))
            return result
        sleep(5)
    # Do not send a second deploy while the first might still be running.
    raise RuntimeError("SSM wait timed out; inspect this command before retrying")


def main():
    digest, commit = os.environ["IMAGE_DIGEST"], os.environ["CI_SHA"]
    script = Path(__file__).with_name("deploy_host.py")
    data = parameters(digest, commit, hashlib.sha256(script.read_bytes()).hexdigest())
    with tempfile.TemporaryDirectory(prefix="finbot-ssm-") as directory:
        file = Path(directory, "parameters.json")
        file.write_text(json.dumps(data))
        sent = aws("ssm", "send-command", "--document-name", "FinBotDeployTradingAgents",
                   "--instance-ids", "i-0cc2d47020ed7347c", "--parameters", f"file://{file}",
                   "--timeout-seconds", "120", "--comment", "FinBot main " + commit)
    command_id = sent["Command"]["CommandId"]
    print("SSM command: " + command_id)
    # Keep the command ID reviewable even if the runner is interrupted.
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
        summary.write(f"SSM command: `{command_id}`\n\nCommit: `{commit}`\n\nDigest: `{digest}`\n")
    wait(command_id)


if __name__ == "__main__":
    main()
