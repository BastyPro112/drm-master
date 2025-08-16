#!/usr/bin/env python3
"""
DRM-master.py (unido)
- Reproductor integrado con python-vlc (para MPD no cifrados).
- Si se proporciona KID:KEY, usa ffplay/ffmpeg para reproducir/grabar Widevine:
    - Reproducir: ffplay -cenc_decryption_key KEY -i "MPD_URL"
    - Grabar:   ffmpeg -cenc_decryption_key KEY -i "MPD_URL" -map 0:v:0 -map 0:a -c copy -f mpegts out.ts
- Mantiene screen-record (ffmpeg region capture) como en el original.
LEGAL: No se proporciona ayuda para eludir protecciones. Usa sólo con permisos.
"""
import sys
import re
import shlex
import subprocess
import signal
import os
import platform
from pathlib import Path
from PyQt6 import QtWidgets, QtGui, QtCore
import vlc

APP_NAME = "DRM-master"

class DRMMaster(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(900, 550)

        # VLC player (integrated fallback for non-DRM)
        self.instance = vlc.Instance()
        self.player = self.instance.media_player_new()

        # Recording states
        self.recording = False
        self.record_path = None

        # Screen-record (ffmpeg) state
        self.screen_rec_process = None
        self.screen_rec_path = None
        self.screen_rec_framerate = 25

        # FFmpeg/ffplay processes for Widevine
        self.ffplay_proc = None
        self.ffmpeg_rec_proc = None  # for recording via ffmpeg when KEY provided

        # UI
        self._build_ui()
        self._connect_signals()

        # UI update timer (for integrated player)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.update_ui)

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(8,8,8,8)

        # Top form: URL + KID:KEY
        form = QtWidgets.QHBoxLayout()
        self.url_edit = QtWidgets.QLineEdit()
        self.url_edit.setPlaceholderText("Introduce enlace .mpd (ej: https://.../manifest.mpd)")
        self.kidkey_edit = QtWidgets.QLineEdit()
        self.kidkey_edit.setPlaceholderText("Introduce KID:KEY (ej: KID:KEY). Si hay KEY, se usará para Widevine.")
        form.addWidget(QtWidgets.QLabel("Stream (.mpd):"))
        form.addWidget(self.url_edit)
        form.addWidget(QtWidgets.QLabel("KID:KEY:"))
        form.addWidget(self.kidkey_edit)
        layout.addLayout(form)

        # Video frame
        self.video_frame = QtWidgets.QFrame()
        self.video_frame.setFrameShape(QtWidgets.QFrame.Shape.Box)
        self.video_frame.setStyleSheet("background: black;")
        layout.addWidget(self.video_frame, stretch=1)

        # Controls row
        controls = QtWidgets.QHBoxLayout()
        self.play_btn = QtWidgets.QPushButton("Play")
        self.pause_btn = QtWidgets.QPushButton("Pause")
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.record_btn = QtWidgets.QPushButton("Record")       # libVLC sout OR ffmpeg when KEY exists
        self.screen_rec_btn = QtWidgets.QPushButton("Screen Rec")# ffmpeg screen capture
        controls.addWidget(self.play_btn)
        controls.addWidget(self.pause_btn)
        controls.addWidget(self.stop_btn)
        controls.addWidget(self.record_btn)
        controls.addWidget(self.screen_rec_btn)

        self.time_label = QtWidgets.QLabel("00:00 / 00:00")
        controls.addWidget(self.time_label)

        self.position_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 1000)
        controls.addWidget(self.position_slider, stretch=1)

        layout.addLayout(controls)

        # Status / info
        self.status = QtWidgets.QLabel("Listo. Nota: Soporta .mpd. Si introduces KID:KEY se usa ffplay/ffmpeg para Widevine.")
        layout.addWidget(self.status)

    def _connect_signals(self):
        self.play_btn.clicked.connect(self.on_play)
        self.pause_btn.clicked.connect(self.on_pause)
        self.stop_btn.clicked.connect(self.on_stop)
        self.record_btn.clicked.connect(self.on_record)
        self.screen_rec_btn.clicked.connect(self.on_screen_record)
        self.position_slider.sliderPressed.connect(self.on_slider_press)
        self.position_slider.sliderReleased.connect(self.on_slider_release)
        self.position_slider.sliderMoved.connect(self.on_slider_move)
        self.kidkey_edit.returnPressed.connect(lambda: self.status.setText("KID:KEY guardado (se usará si es necesario)."))
        self.installEventFilter(self)

    # ---------------- helpers ----------------
    

    def extract_key_after_colon(self, kidkey: str) -> str:
        if ":" in kidkey:
            return kidkey.split(":", 1)[1].strip()
        return ""

    def make_media_with_recording(self, url: str, outpath: Path):
        out = str(outpath.resolve())
        # sout to duplicate: display + file (mp4)
        sout_opt = f":sout=#duplicate{{dst=display,dst=std{{access=file,mux=mp4,dst={out}}}}}"
        media = self.instance.media_new(url, sout_opt)
        return media

    def attach_video(self):
        if sys.platform.startswith("linux"):
            self.player.set_xwindow(self.video_frame.winId())
        elif sys.platform == "win32":
            self.player.set_hwnd(self.video_frame.winId())
        elif sys.platform == "darwin":
            self.player.set_nsobject(int(self.video_frame.winId()))
        else:
            self.player.set_hwnd(self.video_frame.winId())

    def ffplay_available(self):
        return shutil_which("ffplay") is not None

    def ffmpeg_available(self):
        return shutil_which("ffmpeg") is not None

    # ---------------- actions ----------------
    def on_play(self):
        url = self.url_edit.text().strip()
        kidkey = self.kidkey_edit.text().strip()

       

        key = self.extract_key_after_colon(kidkey)
        # If there's a KEY use ffplay to play Widevine; else use integrated libVLC
        if key:
            # Use ffplay (external window) for Widevine decryption playback
            if shutil_which("ffplay") is None:
                QtWidgets.QMessageBox.warning(self, "ffplay no encontrado", "No se encuentra 'ffplay' en PATH. Instala ffmpeg (incluye ffplay).")
                return
            if self.ffplay_proc:
                self.status.setText("ffplay ya está reproduciendo.")
                return
            cmd = ["ffplay", "-loglevel", "error", "-cenc_decryption_key", key, "-i", url]
            try:
                # start ffplay; it will open its own video window
                self.ffplay_proc = subprocess.Popen(cmd)
                self.status.setText(f"Reproduciendo con ffplay (Widevine). PID: {self.ffplay_proc.pid}")
            except Exception as e:
                QtWidgets.QMessageBox.warning(self, "Error ffplay", f"No se pudo iniciar ffplay: {e}")
                self.ffplay_proc = None
            return

        # else: integrated libVLC playback (for non-DRM MPD)
        if kidkey and not key:
            self.status.setText("KID:KEY inválido (se esperaba KID:KEY).")
        elif kidkey:
            # there was input but no key (should not happen because above check), just show status
            self.status.setText("KID:KEY guardado como metadato (no se usa en libVLC).")

        if self.recording and self.record_path:
            media = self.make_media_with_recording(url, Path(self.record_path))
        else:
            media = self.instance.media_new(url)

        self.player.set_media(media)
        self.attach_video()
        res = self.player.play()
        if res == -1:
            self.status.setText("Error al reproducir con libVLC. ¿Stream DRM o URL inaccesible?")
            return

        self.timer.start()
        rec_text = f" (grabando -> {self.record_path})" if self.recording and self.record_path else ""
        screen_text = f" | ScreenRec: {'ON' if self.screen_rec_process else 'OFF'}"
        self.status.setText(f"Reproduciendo (libVLC): {url}{rec_text}{screen_text}")

    def on_pause(self):
        # If ffplay playing, can't pause programmatically — notify.
        if self.ffplay_proc:
            self.status.setText("ffplay no soporta pausa desde aquí. Usa la ventana de ffplay.")
            return
        self.player.pause()
        self.status.setText("Pausado")

    def on_stop(self):
        # stop integrated player
        self.player.stop()
        self.timer.stop()
        self.position_slider.setValue(0)
        self.time_label.setText("00:00 / 00:00")

        # stop ffplay if running
        if self.ffplay_proc:
            try:
                self.ffplay_proc.terminate()
                self.ffplay_proc.wait(timeout=3)
            except Exception:
                try:
                    self.ffplay_proc.kill()
                except Exception:
                    pass
            self.ffplay_proc = None
            self.status.setText("ffplay detenido.")

        # stop ffmpeg recording if active
        if self.ffmpeg_rec_proc:
            try:
                self.ffmpeg_rec_proc.terminate()
                self.ffmpeg_rec_proc.wait(timeout=3)
            except Exception:
                try:
                    self.ffmpeg_rec_proc.kill()
                except Exception:
                    pass
            self.ffmpeg_rec_proc = None

        # stop screen rec if active
        if self.screen_rec_process:
            self._stop_ffmpeg_screenrec()

        # reset libVLC recording state if active
        if self.recording:
            self.recording = False
            self.record_btn.setText("Record")

        self.status.setText("Parado")

    def on_record(self):
        """
        Dual behavior:
         - If KID:KEY provided -> start/stop ffmpeg recording with -cenc_decryption_key (into .ts)
         - Else -> fallback to libVLC sout recording as before
        """
        url = self.url_edit.text().strip()
        kidkey = self.kidkey_edit.text().strip()
        key = self.extract_key_after_colon(kidkey)

        if key:
            # Use ffmpeg recording for Widevine
            if shutil_which("ffmpeg") is None:
                QtWidgets.QMessageBox.warning(self, "ffmpeg no encontrado", "No se encuentra 'ffmpeg' en PATH.")
                return
            if not self.validate_mpd(url):
                QtWidgets.QMessageBox.warning(self, "URL inválida", "Introduce una URL que contenga .mpd.")
                return

            if self.ffmpeg_rec_proc:
                # stop recording
                try:
                    self.ffmpeg_rec_proc.terminate()
                    self.ffmpeg_rec_proc.wait(timeout=5)
                except Exception:
                    try:
                        self.ffmpeg_rec_proc.kill()
                    except Exception:
                        pass
                self.ffmpeg_rec_proc = None
                self.record_btn.setText("Record")
                self.status.setText(f"Grabación ffmpeg detenida. Archivo: {self.record_path}")
                return

            # start recording: ask filename
            fname, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Guardar grabación (ffmpeg) como", "", "TS files (*.ts);;All files (*)")
            if not fname:
                return
            if not Path(fname).suffix:
                fname = fname + ".ts"
            self.record_path = fname
            # build command (user's recommended)
            cmd = [
                "ffmpeg",
                "-y",
                "-loglevel", "error",
                "-cenc_decryption_key", key,
                "-i", url,
                "-map", "0:v:0", "-map", "0:a",
                "-c", "copy", "-f", "mpegts",
                self.record_path
            ]
            try:
                self.ffmpeg_rec_proc = subprocess.Popen(cmd)
            except Exception as e:
                QtWidgets.QMessageBox.warning(self, "Error al iniciar ffmpeg", f"No se pudo iniciar ffmpeg: {e}")
                self.ffmpeg_rec_proc = None
                return
            self.record_btn.setText("Stop Rec")
            self.status.setText(f"Grabando (ffmpeg) -> {self.record_path}")
            return

        # else fallback: original libVLC sout recording
        if not self.recording:
            fname, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Guardar grabación (libVLC) como", "", "MP4 files (*.mp4);;All files (*)")
            if not fname:
                return
            if not Path(fname).suffix:
                fname = fname + ".mp4"
            self.record_path = fname
            self.recording = True
            self.record_btn.setText("Stop Rec")
            self.status.setText(f"Preparado para grabar por libVLC -> {self.record_path}")
            # if already playing, restart with sout
            if self.player.is_playing():
                
                self.player.stop()
                media = self.make_media_with_recording(url, Path(self.record_path))
                self.player.set_media(media)
                self.attach_video()
                self.player.play()
                self.timer.start()
                self.status.setText(f"Grabando (libVLC) -> {self.record_path}")
        else:
            # stop libVLC recording: restart playback without sout
            self.recording = False
            self.record_btn.setText("Record")
            if self.player.is_playing():
                url = self.url_edit.text().strip()
                self.player.stop()
                media = self.instance.media_new(url)
                self.player.set_media(media)
                self.attach_video()
                self.player.play()
                self.status.setText(f"Grabación (libVLC) detenida. Archivo: {self.record_path}")
            else:
                self.status.setText("Grabación (libVLC) cancelada.")

    # ---------------- screen recording (ffmpeg) ----------------
    def on_screen_record(self):
        """Toggle screen recording with ffmpeg capturing video_frame region."""
        if not self.screen_rec_process:
            # start screen rec
            fname, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Guardar grabación (screen) como", "", "MP4 files (*.mp4);;All files (*)")
            if not fname:
                return
            if not Path(fname).suffix:
                fname = fname + ".mp4"
            self.screen_rec_path = fname
            # compute geometry of video_frame in global coordinates
            geo = self._get_video_frame_geometry()
            if geo is None:
                QtWidgets.QMessageBox.warning(self, "Error", "No se puede obtener la geometría de la ventana para screen-record.")
                return
            x, y, w, h = geo
            cmd = self._build_ffmpeg_cmd_for_region(x, y, w, h, self.screen_rec_framerate, self.screen_rec_path)
            if not cmd:
                QtWidgets.QMessageBox.warning(self, "Unsupported OS", "Screen recording no soportado en este sistema.")
                return
            try:
                # start ffmpeg as subprocess
                # use shell=False with list args; keep stderr redirected for now
                self.screen_rec_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                QtWidgets.QMessageBox.warning(self, "Error al iniciar ffmpeg", f"No se pudo iniciar ffmpeg: {e}")
                self.screen_rec_process = None
                return
            self.screen_rec_btn.setText("Stop ScreenRec")
            self.status.setText(f"Grabando pantalla -> {self.screen_rec_path}")
        else:
            # stop
            self._stop_ffmpeg_screenrec()
            self.status.setText(f"Screen recording guardado en {self.screen_rec_path}")

    def _stop_ffmpeg_screenrec(self):
        if self.screen_rec_process:
            try:
                # terminate process nicely
                if platform.system() == "Windows":
                    self.screen_rec_process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    self.screen_rec_process.terminate()
            except Exception:
                pass
            try:
                self.screen_rec_process.wait(timeout=5)
            except Exception:
                try:
                    self.screen_rec_process.kill()
                except Exception:
                    pass
            self.screen_rec_process = None
            self.screen_rec_btn.setText("Screen Rec")

    def _get_video_frame_geometry(self):
        """
        Return (x, y, w, h) in global screen coords for the video_frame content area.
        """
        try:
            top_left = self.video_frame.mapToGlobal(QtCore.QPoint(0, 0))
            x = top_left.x()
            y = top_left.y()
            w = self.video_frame.width()
            h = self.video_frame.height()
            return (x, y, w, h)
        except Exception:
            return None

    def _build_ffmpeg_cmd_for_region(self, x, y, w, h, framerate, outpath):
        """
        Build ffmpeg command (list) to record region (x,y,w,h) according to OS.
        """
        system = platform.system()
        out = str(Path(outpath).resolve())
        if system == "Windows":
            cmd = [
                "ffmpeg",
                "-y",
                "-f", "gdigrab",
                "-framerate", str(framerate),
                "-offset_x", str(x),
                "-offset_y", str(y),
                "-video_size", f"{w}x{h}",
                "-i", "desktop",
                "-pix_fmt", "yuv420p",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                out
            ]
            return cmd
        elif system == "Linux":
            display = os.environ.get("DISPLAY", ":0.0")
            input_str = f"{display}+{x},{y}"
            cmd = [
                "ffmpeg",
                "-y",
                "-f", "x11grab",
                "-framerate", str(framerate),
                "-video_size", f"{w}x{h}",
                "-i", input_str,
                "-pix_fmt", "yuv420p",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                out
            ]
            return cmd
        elif system == "Darwin":
            screen_index = "1"
            cmd = [
                "ffmpeg",
                "-y",
                "-f", "avfoundation",
                "-framerate", str(framerate),
                "-i", f"{screen_index}",
                "-vf", f"crop={w}:{h}:{x}:{y}",
                "-pix_fmt", "yuv420p",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                out
            ]
            return cmd
        else:
            return None

    # ---------------- slider & UI ----------------
    def on_slider_press(self):
        self.timer.stop()

    def on_slider_release(self):
        val = self.position_slider.value() / 1000.0
        self.player.set_position(val)
        self.timer.start()

    def on_slider_move(self, value):
        length = self.player.get_length() / 1000 if self.player.get_length() > 0 else 0
        pos = (value / 1000.0) * length
        self.time_label.setText(f"{self.format_seconds(pos)} / {self.format_seconds(length)}")

    def update_ui(self):
        if self.player is None:
            return
        length_ms = self.player.get_length()
        pos = self.player.get_position()
        time_sec = self.player.get_time() / 1000 if self.player.get_time() != -1 else 0
        total_sec = length_ms / 1000 if length_ms > 0 else 0

        if pos == -1:
            pos = 0
        slider_val = int(pos * 1000)
        self.position_slider.blockSignals(True)
        self.position_slider.setValue(slider_val)
        self.position_slider.blockSignals(False)
        self.time_label.setText(f"{self.format_seconds(time_sec)} / {self.format_seconds(total_sec)}")

    def format_seconds(self, s: float) -> str:
        s = 0 if s is None or s != s or s < 0 else int(s)
        h = s // 3600
        m = (s % 3600) // 60
        sec = s % 60
        if h:
            return f"{h:02d}:{m:02d}:{sec:02d}"
        return f"{m:02d}:{sec:02d}"

    def eventFilter(self, obj, event):
        return super().eventFilter(obj, event)

# small helper (shutil.which wrapped to avoid extra import trouble)
def shutil_which(prog):
    # Try shutil.which if available
    try:
        import shutil
        return shutil.which(prog)
    except Exception:
        # fallback: search PATH
        paths = os.environ.get("PATH", "").split(os.pathsep)
        for p in paths:
            candidate = os.path.join(p, prog)
            if os.path.exists(candidate) and os.access(candidate, os.X_OK):
                return candidate
            # windows .exe appended
            if platform.system() == "Windows":
                candidate_exe = candidate + ".exe"
                if os.path.exists(candidate_exe) and os.access(candidate_exe, os.X_OK):
                    return candidate_exe
        return None

def main():
    app = QtWidgets.QApplication(sys.argv)
    window = DRMMaster()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
