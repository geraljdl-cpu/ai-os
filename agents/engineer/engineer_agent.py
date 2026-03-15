#!/usr/bin/env python3
import subprocess
import sys

def main():
    if len(sys.argv) < 2:
        print("Usage: engineer_agent.py '<goal>'")
        sys.exit(1)

    goal = sys.argv[1]

    cmd = [
        "aider",
        "--model", "ollama/qwen2.5:14b", "--api-base", "http://IP_DO_NODE:11434",
        "--yes",
        "--message", goal,
    ]

    print("Running engineer agent...")
    subprocess.run(cmd, check=False)

if __name__ == "__main__":
    main()
