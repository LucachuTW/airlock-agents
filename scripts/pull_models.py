"""Pull the Ollama models for the detected (or overridden) hardware tier."""

import subprocess
import sys

from app import llm


def main() -> None:
    models = llm.resolve()
    print(f"tier={models['tier']}  chat={models['chat']}  embeddings={models['embeddings']}")
    for model in (models["chat"], models["embeddings"]):
        print(f"--> ollama pull {model}")
        result = subprocess.run(["ollama", "pull", model])
        if result.returncode != 0:
            sys.exit(f"failed to pull {model} — is ollama running?")


if __name__ == "__main__":
    main()
