import os
import time
import psutil
import subprocess
import sys
from flask import Flask, jsonify


class CameraService:
    def __init__(self, watch_target="myapp.exe", check_interval=2, grace_period=10, lock_file="camera_service.lock"):
        self.watch_target = watch_target
        self.check_interval = check_interval
        self.grace_period = grace_period
        self.lock_file = lock_file
        self.missing_time = 0

    # ========== Watchdog Logic ==========
    def is_already_running(self):
        if os.path.exists(self.lock_file):
            try:
                with open(self.lock_file, 'r') as f:
                    pid = int(f.read())
                if psutil.pid_exists(pid):
                    return True
            except:
                pass
        return False

    def write_lock(self):
        with open(self.lock_file, 'w') as f:
            f.write(str(os.getpid()))

    def remove_lock(self):
        try:
            os.remove(self.lock_file)
        except:
            pass

    def is_myapp_running(self):
        for proc in psutil.process_iter(['name']):
            if proc.info['name'] == self.watch_target:
                return True
        return False

    def do_camera_work(self):
        print("[CameraService] Capturing frame...")

    def run_service_loop(self):
        self.write_lock()
        print(f"[CameraService] Started. Watching for {self.watch_target}...")

        try:
            while True:
                if self.is_myapp_running():
                    print(f"[CameraService] {self.watch_target} is running.")
                    self.missing_time = 0
                else:
                    self.missing_time += self.check_interval
                    print(f"[CameraService] {self.watch_target} NOT found... ({self.missing_time}/{self.grace_period}s)")
                    if self.missing_time >= self.grace_period:
                        print("[CameraService] Target app missing too long. Exiting.")
                        break

                self.do_camera_work()
                time.sleep(self.check_interval)
        finally:
            self.remove_lock()
            print("[CameraService] Shutdown complete.")

    def launch_background(self):
        if self.is_already_running():
            print("[CameraService] Already running.")
            return

        print("[Launcher] Launching in background...")
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "run"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP |
                subprocess.DETACHED_PROCESS |
                subprocess.CREATE_NEW_CONSOLE
            )
        )
        print("[Launcher] Background process started.")


# ========== Flask API as Independent App ==========
def create_api_server():
    app = Flask(__name__)
    lock_file = "camera_service.lock"

    def is_myapp_running():
        for proc in psutil.process_iter(['name']):
            if proc.info['name'] == "myapp.exe":
                return True
        return False

    @app.route("/")
    def index():
        return jsonify({"status": "camera API online"})

    @app.route("/status")
    def status():
        return jsonify({"myapp_running": is_myapp_running()})

    @app.route("/lock")
    def lock():
        return jsonify({
            "lock_exists": os.path.exists(lock_file),
            "lock_pid": open(lock_file).read() if os.path.exists(lock_file) else None
        })

    return app


# ========== Entry Point ==========
if __name__ == "__main__":
    if len(sys.argv) > 1:
        mode = sys.argv[1]
        service = CameraService()

        if mode == "run":
            service.run_service_loop()

        elif mode == "launch":
            service.launch_background()

        elif mode == "api":
            app = create_api_server()
            app.run(host="0.0.0.0", port=5566)

        else:
            print(f"[Error] Unknown mode: {mode}")

    else:
        print("Usage:")
        print("  python camera_service.py run      # Run as watchdog service")
        print("  python camera_service.py api      # Start Flask API server")
        print("  python camera_service.py launch   # Run background service")
