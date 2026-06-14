#!/usr/bin/env python3
"""
Stock Analysis Scheduler
Runs the Nsr_trade.py script at regular intervals and logs only the run summary.
"""

import os
import sys
import time
import subprocess
import argparse
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ================= DEFAULT CONFIGURATION =================
DEFAULT_PROJECT_DIR = "/Users/apple/Downloads/PK/visual studio/mahadev core output"
DEFAULT_SCRIPT_NAME = "mahadev/Zerodha connect/Nsr_trade.py"
DEFAULT_INTERVAL_MINUTES = 4
DEFAULT_TIMEOUT_SECONDS = 700
DEFAULT_LOG_FILE = "/Users/apple/Desktop/scheduler_run.log"
DEFAULT_PYTHON_EXEC = "myenv/bin/python3"

# ================= SETUP LOGGING =================
def setup_logging(log_file, max_bytes=5*1024*1024, backup_count=3):
    logger = logging.getLogger('scheduler')
    logger.setLevel(logging.INFO)
    file_handler = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count)
    console_handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger

# ================= RUN FUNCTION (FILTERED) =================
def run_analysis(project_dir, python_path, script_path, logger, timeout_sec):
    timestamp_start = datetime.now()
    script_name = script_path.name
    logger.info("=" * 60)
    logger.info(f"Starting analysis run at {timestamp_start.strftime('%Y-%m-%d %H:%M:%S')} - Script: {script_name}")
    
    os.chdir(project_dir)
    
    try:
        result = subprocess.run(
            [python_path, script_path],
            capture_output=True,
            text=True,
            timeout=timeout_sec
        )
        duration = datetime.now() - timestamp_start
        
        # Extract only the summary lines
        if result.stdout:
            lines = result.stdout.strip().split('\n')
            summary_lines = []
            in_summary = False
            
            for line in lines:
                if not line.strip():
                    continue
                if "📊 RUN SUMMARY" in line:
                    in_summary = True
                    summary_lines.append(line)
                elif in_summary:
                    summary_lines.append(line)
                    # Optional: stop at the end marker if needed (but keep all)
            
            # Fallback: if no summary marker, take last 20 lines
            if not summary_lines and lines:
                summary_lines = lines[-20:]
            
            if summary_lines:
                logger.info("Run summary:")
                for line in summary_lines:
                    logger.info(f"  {line}")
            else:
                logger.info("No summary output captured.")
        
        if result.stderr:
            logger.error(f"Script stderr:\n{result.stderr}")
        
        if result.returncode == 0:
            logger.info(f"✅ Analysis completed successfully in {duration.total_seconds():.1f}s")
        else:
            logger.warning(f"⚠️ Analysis finished with return code {result.returncode}")
        
        return True
    except subprocess.TimeoutExpired:
        logger.error(f"❌ Script timed out after {timeout_sec} seconds")
        return False
    except Exception as e:
        logger.error(f"❌ Unexpected error: {e}")
        return False

# ================= MAIN =================
def main():
    parser = argparse.ArgumentParser(description='Stock analysis scheduler (summary only)')
    parser.add_argument('--interval', type=int, default=DEFAULT_INTERVAL_MINUTES)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--project-dir', type=str, default=DEFAULT_PROJECT_DIR)
    parser.add_argument('--script', type=str, default=DEFAULT_SCRIPT_NAME)
    parser.add_argument('--python', type=str, default=DEFAULT_PYTHON_EXEC)
    parser.add_argument('--log-file', type=str, default=DEFAULT_LOG_FILE)
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument('--quiet', action='store_true')
    
    args = parser.parse_args()
    
    project_dir = Path(args.project_dir).resolve()
    python_path = project_dir / args.python
    script_path = project_dir / args.script
    
    if not project_dir.exists():
        print(f"❌ Project directory not found: {project_dir}")
        sys.exit(1)
    if not python_path.exists():
        print(f"❌ Python executable not found: {python_path}")
        sys.exit(1)
    if not script_path.exists():
        print(f"❌ Script not found: {script_path}")
        sys.exit(1)
    
    logger = setup_logging(args.log_file)
    if args.quiet:
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler):
                logger.removeHandler(handler)
    
    logger.info("=" * 60)
    logger.info("📊 Stock Analysis Scheduler Started")
    logger.info(f"   Project dir : {project_dir}")
    logger.info(f"   Python exe   : {python_path}")
    logger.info(f"   Script       : {script_path}")
    logger.info(f"   Interval     : {args.interval} minute(s)")
    logger.info(f"   Timeout      : {args.timeout}s")
    logger.info(f"   Log file     : {args.log_file}")
    logger.info(f"   Run once     : {args.once}")
    logger.info("=" * 60)
    
    run_analysis(project_dir, python_path, script_path, logger, args.timeout)
    
    if args.once:
        logger.info("Run-once mode completed. Exiting.")
        return
    
    try:
        import schedule
    except ImportError:
        logger.error("❌ 'schedule' library not installed. Install with: pip install schedule")
        sys.exit(1)
    
    schedule.every(args.interval).minutes.do(
        run_analysis, project_dir, python_path, script_path, logger, args.timeout
    )
    
    logger.info(f"🕒 Scheduler running every {args.interval} minute(s). Press Ctrl+C to stop.")
    
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("⏹️ Scheduler stopped by user.")

if __name__ == "__main__":
    main()