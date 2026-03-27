#!/bin/bash
# PCB-Creator Install Script
# Sets up Python environment, dependencies, and configuration

set -e

echo "=== PCB-Creator Setup ==="
echo ""

# Check Python version
PYTHON=""
for cmd in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.11+ required. Install from https://python.org"
    exit 1
fi
echo "Python: $($PYTHON --version)"

# Check Java (needed for Freerouting)
if command -v java &>/dev/null; then
    java_ver=$(java -version 2>&1 | head -1)
    echo "Java: $java_ver"
    # Check if Java 17+
    java_major=$(java -version 2>&1 | head -1 | sed 's/.*"\([0-9]*\).*/\1/')
    if [ "$java_major" -lt 17 ] 2>/dev/null; then
        echo "WARNING: Java 17+ recommended for Freerouting autorouter."
        echo "  Install: brew install temurin  (macOS)"
        echo "  The built-in router will be used as fallback."
    fi
else
    echo "WARNING: Java not found. Freerouting autorouter won't work."
    echo "  Install: brew install temurin  (macOS)"
    echo "  The built-in router will be used as fallback."
fi

# Create virtual environment
if [ ! -d ".venv" ]; then
    echo ""
    echo "Creating virtual environment..."
    $PYTHON -m venv .venv
fi

# Activate
source .venv/bin/activate
echo "Virtual env: $(which python)"

# Install dependencies
echo ""
echo "Installing dependencies..."
pip install -e ".[dxf]" --quiet

# Create .env if it doesn't exist
if [ ! -f ".env" ]; then
    echo ""
    echo "Creating .env configuration file..."
    cat > .env << 'ENVEOF'
# PCB-Creator Configuration
# Uncomment and edit the settings you need.

# === LLM Provider ===
# OpenRouter (default — requires API key from https://openrouter.ai)
# PCB_GENERATE_MODEL=openrouter/qwen/qwen3.5-27b
# PCB_LLM_API_KEY=sk-or-...

# Ollama (local, free — install from https://ollama.com)
# PCB_GENERATE_MODEL=ollama/qwen3.5:27b
# PCB_LLM_API_BASE=http://localhost:11434/v1

# oMLX (local Mac inference)
# PCB_GENERATE_MODEL=openai/Qwen3.5-27B-MLX-7bit
# PCB_LLM_API_BASE=http://localhost:8000/v1

# OpenAI
# PCB_GENERATE_MODEL=openai/gpt-4o
# PCB_LLM_API_KEY=sk-...

# === Router ===
# PCB_ROUTER_ENGINE=freerouting    # or "builtin"
# PCB_FREEROUTING_TIMEOUT=300      # seconds

# === Advanced ===
# PCB_REVIEW_MODEL=                 # separate model for QA review (defaults to generate model)
# PCB_GATHER_MODEL=                 # separate model for requirements gathering
# PCB_MAX_REWORK=5                  # max rework attempts per step
# PCB_ENABLE_OPTIMIZER=true         # enable placement optimizer
ENVEOF
    echo "  Created .env — edit this file to configure your LLM provider."
else
    echo ".env already exists — skipping."
fi

# Create projects directory
mkdir -p projects

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Quick start:"
echo "  source .venv/bin/activate"
echo "  export PCB_LLM_API_KEY=your-key-here     # or edit .env"
echo "  pcb-creator design --project my_board"
echo ""
echo "Or with a requirements file:"
echo "  pcb-creator run --requirements tests/test_switch_led.json --project test1"
echo ""
echo "For local models (Ollama):"
echo "  export PCB_LLM_API_BASE=http://localhost:11434/v1"
echo "  export PCB_GENERATE_MODEL=ollama/qwen3.5:27b"
echo ""
