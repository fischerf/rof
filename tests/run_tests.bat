@echo off
REM run_tests.bat
REM Windows batch script to run all ROF framework tests

echo ================================
echo ROF Framework Test Suite
echo ================================
echo.

REM Check if Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    exit /b 1
)

REM Check if pytest is installed
python -m pytest --version >nul 2>&1
if errorlevel 1 (
    echo WARNING: pytest not found. Installing pytest...
    python -m pip install pytest
)

REM Run the test suite
python run_all_tests.py

REM Capture exit code
set TEST_EXIT_CODE=%ERRORLEVEL%

echo.
echo ================================
echo Test execution complete
echo ================================

exit /b %TEST_EXIT_CODE%
