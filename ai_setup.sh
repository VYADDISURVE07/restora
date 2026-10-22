#!/usr/bin/env sh
# Restora - install the free local AI (Ollama + a small open model). Optional: Restora works without it.
#   sh ai_setup.sh               installs qwen3:4b-instruct (about 2.5 GB)
#   sh ai_setup.sh qwen3:1.7b    a smaller model for older machines
set -e
MODEL="${1:-qwen3:4b-instruct}"
if ! command -v ollama >/dev/null 2>&1; then
  case "$(uname -s)" in
    Darwin) echo "Install Ollama from https://ollama.com/download (or: brew install ollama), then run this again."; exit 1 ;;
    *) echo "Installing Ollama with its official script from https://ollama.com/install.sh ..."
       curl -fsSL https://ollama.com/install.sh | sh ;;
  esac
fi
if ! curl -s http://localhost:11434/api/tags >/dev/null 2>&1; then
  echo "Starting the Ollama server..."; (ollama serve >/dev/null 2>&1 &) ; sleep 3
fi
echo "Downloading the model $MODEL (once)..."
ollama pull "$MODEL"
echo "Checking it works..."
python3 run.py --ai-check --model "$MODEL" 2>/dev/null || .venv/bin/python run.py --ai-check --model "$MODEL"
