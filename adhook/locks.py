"""Process-wide locks. The box has 2 vCPUs, so local CPU work (Whisper, ffmpeg) runs one at a time."""
import threading

CPU_LOCK = threading.Lock()
