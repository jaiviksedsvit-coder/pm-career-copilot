@echo off
setlocal

REM =============================================================================
REM PM Career Copilot - Master Launcher
REM Double-click this file or run from command line.
REM =============================================================================

REM Ensure MiKTeX pdflatex is on PATH
set "MIKTEX_BIN=%LOCALAPPDATA%\Programs\MiKTeX\miktex\bin\x64"
if exist "%MIKTEX_BIN%\pdflatex.exe" (
    set "PATH=%PATH%;%MIKTEX_BIN%"
)

cd /d "%~dp0"

REM If flags or JD arguments passed directly
if not "%~1"=="" (
    if "%~1:~0,2%"=="--" (
        python daily_hunt.py %*
        goto :end
    )
    set "JD_FILE=%~1"
    set "COMPANY_NAME=%~2"
    if "%COMPANY_NAME%"=="" set "COMPANY_NAME=TargetCompany"
    python dossier_bridge.py --jd-file "%JD_FILE%" --company "%COMPANY_NAME%" --dry-run
    goto :end
)

:menu
cls
echo =============================================================================
echo   PM CAREER COPILOT - MORNING HITL LAUNCHER
echo   Candidate: Jaivik Chauhan ^| Target: APM / PM-1 India
echo =============================================================================
echo.
echo   1. Morning Hunt (Live: Ingest + Triage + Compile + Gmail Staging + Open Links)
echo   2. Morning Hunt (Dry-Run: No Live Gmail API Drafts)
echo   3. Launch Streamlit Dashboard (Product Analytics & Pipeline Tracker)
echo   4. View Database Audit Trail (CLI Stats)
echo   5. Authorize Gmail API (One-Time OAuth Setup)
echo   6. Exit
echo.
set /p "CHOICE=Enter choice [1-6]: "

if "%CHOICE%"=="1" (
    echo.
    echo [RUN] Launching Live Morning Hunt with browser link opening...
    python daily_hunt.py --open-urls
    pause
    goto :menu
)
if "%CHOICE%"=="2" (
    echo.
    echo [RUN] Launching Dry-Run Morning Hunt...
    python daily_hunt.py --dry-run
    pause
    goto :menu
)
if "%CHOICE%"=="3" (
    echo.
    echo [RUN] Starting Streamlit Dashboard on http://localhost:8501...
    streamlit run dashboard.py
    goto :menu
)
if "%CHOICE%"=="4" (
    echo.
    python daily_hunt.py --stats
    pause
    goto :menu
)
if "%CHOICE%"=="5" (
    echo.
    python auth_gmail_server.py --account primary
    pause
    goto :menu
)
if "%CHOICE%"=="6" (
    goto :end
)

:end
endlocal
