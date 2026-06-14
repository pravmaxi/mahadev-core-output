import schedule
import time
import subprocess
import sys
import os
from datetime import datetime

def run_bot():
    timestamp = datetime.now().strftime('%H:%M:%S')
    print(f"\n[{'='*60}]")
    print(f"[{timestamp}] Running stock bot...")
    
    # Use absolute paths
    project_dir = "/Users/apple/Downloads/PK/visual studio/mahadev core output"
    python_path = os.path.join(project_dir, "myenv/bin/python3")
    script_path = os.path.join(project_dir, "mahadev/Zerodha connect/Nsr_trade.py")
    
    # Log file
    log_file = "/Users/apple/Desktop/scheduler_run.log"
    
    try:
        # Change to project directory
        os.chdir(project_dir)
        
        # Run the script
        result = subprocess.run(
            [python_path, script_path],
            capture_output=True,
            text=True,
            timeout=120  # 2 minute timeout
        )
        
        
        # Log results
        with open(log_file, "a") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"Run started: {timestamp}\n")
            f.write(f"Run completed: {datetime.now().strftime('%H:%M:%S')}\n")
            f.write(f"Duration: {datetime.now() - datetime.strptime(timestamp, '%H:%M:%S')}\n")
            
            if result.stdout:
                f.write(f"Output (last 10 lines):\n")
                for line in result.stdout.strip().split('\n')[-10:]:
                    f.write(f"  {line}\n")
            
            if result.stderr:
                f.write(f"Errors:\n{result.stderr}\n")
        
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Completed successfully")
        print(f"[{'='*60}]")
        
    except subprocess.TimeoutExpired:
        error_msg = "Script timed out after 2 minutes"
        print(f"ERROR: {error_msg}")
        with open(log_file, "a") as f:
            f.write(f"\nERROR: {error_msg}\n")
    except Exception as e:
        error_msg = f"Unexpected error: {e}"
        print(f"ERROR: {error_msg}")
        with open(log_file, "a") as f:
            f.write(f"\nERROR: {error_msg}\n")

if __name__ == "__main__":
    print("Stock Bot Scheduler Started")
    print("=" * 60)
    print(f"Project: /Users/apple/Downloads/PK/visual studio/mahadev core output")
    print(f"Python: /Users/apple/Downloads/PK/visual studio/mahadev core output/myenv/bin/python3")
    print(f"Script: 4hrbot1 copy.py")
    print(f"Log: ~/Desktop/scheduler_run.log")
    print(f"Schedule: Every 4 minutes")
    print("=" * 60)
    print("Press Ctrl+C to stop\n")
    
    # Run immediately
    run_bot()
    
    # Schedule every 2 minutes (you said 2 minutes in your code)
    schedule.every(4).minutes.do(run_bot)
    
    # Or for every 5 minutes, use:
    # schedule.every(5).minutes.do(run_bot)
    
    # Keep running
    while True:
        schedule.run_pending()
        time.sleep(1)  # Check every second