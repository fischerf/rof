#!/bin/bash
# run_tests.sh
# Unix/Linux/Mac shell script to run all ROF framework tests

echo "================================"
echo "ROF Framework Test Suite"
echo "================================"
echo

# Check if Python is available
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 is not installed or not in PATH"
    exit 1
fi

# Check if pytest is installed
if ! python3 -m pytest --version &> /dev/null; then
    echo "WARNING: pytest not found. Installing pytest..."
    python3 -m pip install pytest
fi

# Run the test suite
python3 run_all_tests.py

# Capture exit code
TEST_EXIT_CODE=$?

echo
echo "================================"
echo "Test execution complete"
echo "================================"

exit $TEST_EXIT_CODE
