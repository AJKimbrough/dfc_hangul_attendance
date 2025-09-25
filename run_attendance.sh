#!/bin/bash
set -a  # auto-export variables

# Load .env safely (ignore comments/empty lines)
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

set +a

# Activate virtualenv
source .venv/bin/activate

# Default port 8000 (override with arg, e.g. ./run_attendance.sh 9001)
PORT=${1:-8000}

# Run Flask
python -m flask --app app.py run --host=0.0.0.0 --port=$PORT
