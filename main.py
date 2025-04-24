import cv2
import logging
import numpy as np
import os
import psutil
import subprocess
import sys
import time
from flask import Flask, jsonify
from multiprocessing import shared_memory

class CameraService:
    def __init__(self,
                 target_process_name="redis-server.exe",
                 check_interval_seconds=2,
                 allowed_missing_duration=10,
                 lock_file_path="camera_service.lock",
                 camera_device_ids=[0, 1],
                 expected_frame_shape=(480, 640, 3)):

        # Set up logger
        self.logger = logging.getLogger(f"CameraService[{os.getpid()}]")
        if not self.logger.hasHandlers():
            self.logger.setLevel(logging.DEBUG)
            formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s')
            file_handler = logging.FileHandler("camera_service.log")
            file_handler.setFormatter(formatter)
            stream_handler = logging.StreamHandler()
            stream_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)
            self.logger.addHandler(stream_handler)

        self.target_process_name = target_process_name
        self.check_interval = check_interval_seconds
        self.max_missing_time = allowed_missing_duration
        self.lock_file_path = lock_file_path
        self.elapsed_missing_time = 0
        self.frame_shape = expected_frame_shape
        self.camera_ids = camera_device_ids

        self.video_captures = []
        self.shared_memory_blocks = []
        self.shared_frame_arrays = []

        try:
            for cam_id in self.camera_ids:
                capture = cv2.VideoCapture(cam_id)
                if not capture.isOpened():
                    self.logger.warning(f"Camera {cam_id} could not be opened.")
                shm_name = f"camera_frame_{cam_id}"
                shm = shared_memory.SharedMemory(create=True, size=np.prod(expected_frame_shape), name=shm_name)
                shared_array = np.ndarray(expected_frame_shape, dtype=np.uint8, buffer=shm.buf)

                self.video_captures.append(capture)
                self.shared_memory_blocks.append(shm)
                self.shared_frame_arrays.append(shared_array)

        except Exception as e:
            self.logger.exception("Failed to initialize camera service:")
            self.release_resources()
            raise

    def is_service_already_running(self):
        if os.path.exists(self.lock_file_path):
            try:
                with open(self.lock_file_path, 'r') as file:
                    pid = int(file.read())
                if psutil.pid_exists(pid):
                    return True
            except Exception as e:
                self.logger.warning("Could not verify existing lock file: %s", e)
        return False

    def create_lock_file(self):
        try:
            with open(self.lock_file_path, 'w') as file:
                file.write(str(os.getpid()))
        except Exception as e:
            self.logger.error("Failed to write lock file: %s", e)
            raise

    def delete_lock_file(self):
        try:
            os.remove(self.lock_file_path)
        except FileNotFoundError:
            pass
        except Exception as e:
            self.logger.warning("Could not delete lock file: %s", e)

    def is_target_process_running(self):
        try:
            for proc in psutil.process_iter(['name']):
                if proc.info['name'] == self.target_process_name:
                    return True
        except Exception as e:
            self.logger.error("Error while checking process list: %s", e)
        return False

    def capture_camera_frames(self):
        for index, capture in enumerate(self.video_captures):
            try:
                success, frame = capture.read()
                if not success:
                    self.logger.warning(f"Failed to read frame from camera {self.camera_ids[index]}")
                    continue

                if frame.shape != self.frame_shape:
                    frame = cv2.resize(frame, (self.frame_shape[1], self.frame_shape[0]))

                self.shared_frame_arrays[index][:] = frame
            except Exception as e:
                self.logger.error(f"Error capturing from camera {self.camera_ids[index]}: {e}")

    def start_service_loop(self):
        self.create_lock_file()
        self.logger.info(f"Service started. Monitoring '{self.target_process_name}'...")

        try:
            while True:
                if self.is_target_process_running():
                    self.logger.debug(f"'{self.target_process_name}' is running.")
                    self.elapsed_missing_time = 0
                else:
                    self.elapsed_missing_time += self.check_interval
                    self.logger.warning(f"'{self.target_process_name}' NOT found... "
                                        f"({self.elapsed_missing_time}/{self.max_missing_time}s)")

                    if self.elapsed_missing_time >= self.max_missing_time:
                        self.logger.error("Target process missing too long. Exiting.")
                        break

                self.capture_camera_frames()
                time.sleep(self.check_interval)
        except KeyboardInterrupt:
            self.logger.info("Interrupted by user.")
        except Exception as e:
            self.logger.exception("Unexpected error during service loop:")
        finally:
            self.release_resources()

    def release_resources(self):
        self.logger.info("Releasing camera and shared memory resources...")
        for capture in self.video_captures:
            try:
                capture.release()
            except Exception as e:
                self.logger.warning(f"Error releasing camera: {e}")
        for shm in self.shared_memory_blocks:
            try:
                shm.close()
                shm.unlink()
            except Exception as e:
                self.logger.warning(f"Error cleaning shared memory: {e}")
        self.delete_lock_file()
        self.logger.info("Cleanup complete.")

    def launch_as_background_process(self, command_args=[sys.executable, os.path.abspath(__file__), "run"]):
        if self.is_service_already_running():
            self.logger.info("Camera service is already running.")
            return

        try:
            self.logger.info(f"Launching background process: {command_args}")
            subprocess.Popen(
                command_args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP |
                    subprocess.DETACHED_PROCESS
                )
            )
            self.logger.info("Background process started.")
        except Exception as e:
            self.logger.error("Failed to launch background process: %s", e)


# ========== Flask API as Independent App ==========
def create_api_server():
    app = Flask(__name__)
    lock_file = "camera_service.lock"

    def is_myapp_running():
        for proc in psutil.process_iter(['name']):
            if proc.info['name'] == "redis-server.exe":
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
