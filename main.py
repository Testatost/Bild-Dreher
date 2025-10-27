import sys
import os
import io
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional
from PIL import Image, ImageOps

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QPoint
from PyQt6.QtGui import QPixmap, QAction
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFileDialog, QTableWidget, QTableWidgetItem, QAbstractItemView,
    QHeaderView, QToolButton, QGridLayout, QSplitter, QProgressBar,
    QMessageBox, QCheckBox, QMenu, QFrame
)

try:
    import piexif
except Exception:
    piexif = None

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}

def is_image_file(p: Path) -> bool:
    return p.suffix.lower() in SUPPORTED_EXTS

def walk_images(paths: List[Path]) -> List[Path]:
    out = []
    for p in paths:
        if p.is_dir():
            for root, _, files in os.walk(p):
                for f in files:
                    fp = Path(root) / f
                    if is_image_file(fp):
                        out.append(fp)
        elif p.is_file() and is_image_file(p):
            out.append(p)
    seen, unique = set(), []
    for p in out:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            unique.append(p)
    return unique

def load_exif_bytes(img: Image.Image) -> Optional[bytes]:
    return img.info.get("exif", None)

def ensure_orientation_reset(exif_bytes: Optional[bytes]) -> Optional[bytes]:
    if exif_bytes and piexif:
        try:
            exif_dict = piexif.load(exif_bytes)
            if "0th" in exif_dict and piexif.ImageIFD.Orientation in exif_dict["0th"]:
                exif_dict["0th"][piexif.ImageIFD.Orientation] = 1
            return piexif.dump(exif_dict)
        except Exception:
            return exif_bytes
    return exif_bytes


@dataclass
class EditStep:
    kind: str
    value: Optional[float] = None


@dataclass
class ImageItem:
    path: Path
    steps: List[EditStep] = field(default_factory=list)
    active: bool = True
    restore_to_baseline: bool = False
    baseline_bytes: bytes = b""
    original_bytes: Optional[bytes] = None  # 🟢 NEU: echter Originalzustand beim ersten Laden

    def set_baseline_from_disk(self):
        self.baseline_bytes = Path(self.path).read_bytes()
        # Wenn Originalzustand noch nicht gespeichert → hier merken
        if self.original_bytes is None:
            self.original_bytes = self.baseline_bytes

    def reset_steps(self):
        self.steps.clear()

    def add_rotation(self, deg: float):
        self.restore_to_baseline = False
        self.steps.append(EditStep("rot", float(deg)))
        self._simplify()

    def mirror_h(self):
        self.restore_to_baseline = False
        self.steps.append(EditStep("mirror_h"))
        self._simplify()

    def original_state(self):
        """🟢 'Original Zustand' = zurück zum allerersten Ladezustand"""
        self.restore_to_baseline = True
        self.steps.clear()

    def is_edited(self) -> bool:
        return self.restore_to_baseline or len(self.steps) > 0

    def _simplify(self):
        rot_sum = 0.0
        mirror = 0
        for s in self.steps:
            if s.kind == "rot":
                rot_sum += float(s.value or 0.0)
            elif s.kind == "mirror_h":
                mirror += 1
        rot = rot_sum % 360
        rot = min([0, 90, 180, 270], key=lambda x: abs(x - rot))
        mirror = mirror % 2
        self.steps = []
        if mirror:
            self.steps.append(EditStep("mirror_h"))
        if rot != 0:
            self.steps.append(EditStep("rot", float(rot)))

    def apply_to(self, img: Image.Image) -> Image.Image:
        out = img.copy()
        for s in self.steps:
            if s.kind == "mirror_h":
                out = ImageOps.mirror(out)
            elif s.kind == "rot":
                out = out.rotate(-float(s.value), expand=True, resample=Image.Resampling.BICUBIC)
        return out


class SaveWorker(QThread):
    progress = pyqtSignal(int, str)
    done = pyqtSignal()

    def __init__(self, items: List[ImageItem], dest_copy: bool, dest_dir: Optional[Path]):
        super().__init__()
        self.items = items
        self.dest_copy = dest_copy
        self.dest_dir = dest_dir
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        for i, item in enumerate(self.items):
            if self._stop:
                self.progress.emit(i, "Abgebrochen")
                break
            if not item.active:
                self.progress.emit(i, "Übersprungen")
                continue
            try:
                # 🟢 Wenn "Original Zustand" aktiviert ist → Originalbytes speichern
                if item.restore_to_baseline and item.original_bytes:
                    out_path = (self.dest_dir / item.path.name) if self.dest_copy and self.dest_dir else item.path
                    out_path.write_bytes(item.original_bytes)
                    self.progress.emit(i, "Gespeichert (Originalzustand)")
                else:
                    im = Image.open(item.path); im.load()
                    im = ImageOps.exif_transpose(im)
                    exif_bytes = ensure_orientation_reset(load_exif_bytes(im))
                    out = item.apply_to(im)
                    out_path = (self.dest_dir / item.path.name) if self.dest_copy and self.dest_dir else item.path
                    save_kwargs = {}
                    if exif_bytes:
                        save_kwargs["exif"] = exif_bytes
                    out.save(out_path, **save_kwargs)
                    im.close(); out.close()
                    self.progress.emit(i, "Gespeichert")
            except Exception as e:
                self.progress.emit(i, f"Fehler: {e}")
        self.done.emit()


class ImageTable(QTableWidget):
    files_dropped = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(0, 4, parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        e.acceptProposedAction()

    def dropEvent(self, e):
        paths = []
        for url in e.mimeData().urls():
            p = Path(url.toLocalFile())
            paths.append(p)
        self.files_dropped.emit(paths)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("🖼️ Bild-Dreher Pro – stabil+")
        self.resize(1280, 780)

        self.items: List[ImageItem] = []
        self.dest_dir: Optional[Path] = None
        self.worker: Optional[SaveWorker] = None
        self.current_row: int = -1
        self.master_checked: bool = True

        # Tabelle
        self.table = ImageTable()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["✓", "Datei", "Aktionen", "Status"])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.files_dropped.connect(self.on_files_dropped)
        self.table.itemSelectionChanged.connect(self.on_selection_changed)
        hdr.sectionClicked.connect(self.on_header_clicked)
        self.table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # Vorschau
        self.preview = QLabel("Vorschau")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(420, 420)
        self.preview.setStyleSheet("border:1px solid #777; border-radius:6px;")

        splitter = QSplitter()
        splitter.addWidget(self.table)
        splitter.addWidget(self.preview)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        # 🔹 Layout oben
        top = QVBoxLayout()
        first_row = QHBoxLayout()

        # Dateien/Ordner hinzufügen
        self.btn_add = QPushButton("Dateien/Ordner hinzufügen")
        add_menu = QMenu(self.btn_add)
        act_files = QAction("Dateien wählen", self)
        act_dir = QAction("Ordner wählen", self)
        act_files.triggered.connect(self.add_files_dialog)
        act_dir.triggered.connect(self.add_dir_dialog)
        add_menu.addAction(act_files); add_menu.addAction(act_dir)
        self.btn_add.setMenu(add_menu)
        first_row.addWidget(self.btn_add)

        top.addLayout(first_row)

        # 🔹 Checkbox + Zielordner untereinander
        copy_box = QVBoxLayout()
        self.chk_copy = QCheckBox("Kopien im Zielordner speichern")
        self.btn_dest = QPushButton("Zielordner wählen")
        self.btn_dest.setEnabled(False)
        self.chk_copy.toggled.connect(self.btn_dest.setEnabled)
        self.btn_dest.clicked.connect(self.choose_dest)
        copy_box.addWidget(self.chk_copy)
        copy_box.addWidget(self.btn_dest)

        # 🔹 zweite Zeile mit den Bearbeitungsbuttons
        edit_row = QHBoxLayout()
        edit_row.addLayout(copy_box)

        def vline():
            line = QFrame()
            line.setFrameShape(QFrame.Shape.VLine)
            line.setFrameShadow(QFrame.Shadow.Sunken)
            return line

        edit_row.addWidget(self._mk_top_btn("+90°", lambda: self.mass_apply("rot", 90)))
        edit_row.addWidget(self._mk_top_btn("−90°", lambda: self.mass_apply("rot", -90)))
        edit_row.addWidget(self._mk_top_btn("180°", lambda: self.mass_apply("rot", 180)))
        edit_row.addWidget(self._mk_top_btn("↔   (spiegeln)", lambda: self.mass_apply("mirror_h", None)))
        edit_row.addWidget(vline())  # 🔹 Trennung Spiegeln / Original Zustand
        edit_row.addWidget(self._mk_top_btn("Original Zustand", self.mass_original))
        edit_row.addWidget(self._mk_top_btn("Reset", self.reset_all))
        edit_row.addWidget(vline())  # 🔹 Trennung Reset / Start
        self.btn_start = self._mk_top_btn("Start", self.start_save)
        self.btn_stop  = self._mk_top_btn("Stopp", self.stop_save); self.btn_stop.setEnabled(False)
        edit_row.addWidget(self.btn_start)
        edit_row.addWidget(self.btn_stop)

        top.addLayout(edit_row)
        top.addWidget(QProgressBar(), stretch=0)

        # 🔹 Hauptlayout
        layout = QVBoxLayout()
        layout.addLayout(top)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        layout.addWidget(splitter)

        # 🔹 "Alle löschen" unter der Tabelle
        self.btn_clear_all = QPushButton("Alle Löschen")
        self.btn_clear_all.setFixedHeight(32)
        self.btn_clear_all.clicked.connect(self.clear_all)
        layout.addWidget(self.btn_clear_all)

        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)
    # --- (restlicher Code bleibt unverändert, identisch mit deiner Version) ---


    # Helper
    def _mk_top_btn(self, label, cb):
        b = QPushButton(label)
        b.setFixedHeight(36)
        b.setAutoRepeat(False)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # << wichtig
        b.clicked.connect(cb)
        return b

    # Dateien / Ordner
    def add_files_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Bilder auswählen", "", "Bilder (*.jpg *.jpeg *.png *.bmp *.tif *.tiff *.webp)"
        )
        self.add_paths([Path(f) for f in files])

    def add_dir_dialog(self):
        d = QFileDialog.getExistingDirectory(self, "Ordner wählen", "")
        if d:
            self.add_paths([Path(d)])

    def on_files_dropped(self, paths):
        self.add_paths(paths)

    def add_paths(self, paths: List[Path]):
        imgs = walk_images(paths)
        added = False
        for p in imgs:
            if any(it.path.resolve() == p.resolve() for it in self.items):
                continue
            self._add_row(p)
            added = True
        if added and self.current_row == -1 and self.items:
            self.table.selectRow(0)
            self.table.setFocus()
            self.table.setCurrentCell(0, 1)  # statt (0, 0)

    def _add_row(self, path: Path):
        row = self.table.rowCount()
        self.table.insertRow(row)
        item = ImageItem(path)
        item.set_baseline_from_disk()
        self.items.insert(row, item)

        chk = QTableWidgetItem()
        chk.setCheckState(Qt.CheckState.Checked)
        chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        self.table.setItem(row, 0, chk)

        name_item = QTableWidgetItem(path.name)
        name_item.setToolTip(str(path))
        self.table.setItem(row, 1, name_item)

        widget = QWidget()
        grid = QGridLayout(widget); grid.setContentsMargins(1,1,1,1); grid.setSpacing(2)

        def cell_btn(txt, tip, slot):
            b = QToolButton()
            b.setText(txt);
            b.setToolTip(tip);
            b.setFixedSize(35, 25)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # << wichtig
            b.clicked.connect(lambda _, s=b: slot(self._row_of_cell_button(s)))
            return b

        grid.addWidget(cell_btn("+90", "90° CW", self._row_act_p90),    0, 0)
        grid.addWidget(cell_btn("−90", "−90° CCW", self._row_act_m90),  0, 1)
        grid.addWidget(cell_btn("180", "180°", self._row_act_180),      0, 2)
        grid.addWidget(cell_btn("↔",   "Horizontal spiegeln", self._row_act_mirror_h), 0, 3)
        grid.addWidget(cell_btn("Undo","Original", self._row_act_undo), 0, 4)
        self.table.setCellWidget(row, 2, widget)

        self.table.setItem(row, 3, QTableWidgetItem("Original"))

    def _row_of_cell_button(self, btn: QToolButton) -> int:
        cell_widget = btn.parentWidget()
        p = self.table.viewport().mapFromGlobal(cell_widget.mapToGlobal(QPoint(0, 0)))
        return self.table.indexAt(p).row()

    # Auswahl/Vorschau
    def on_selection_changed(self):
        rows = self.table.selectionModel().selectedRows()
        self.current_row = rows[0].row() if rows else -1
        self.update_preview()

    # Einzelaktionen (Zell-Buttons)
    def _row_act_p90(self, r): self._ensure_row(r); self.items[r].add_rotation(90); self._touch_row(r)
    def _row_act_m90(self, r): self._ensure_row(r); self.items[r].add_rotation(-90); self._touch_row(r)
    def _row_act_180(self, r): self._ensure_row(r); self.items[r].add_rotation(180); self._touch_row(r)
    def _row_act_mirror_h(self, r): self._ensure_row(r); self.items[r].mirror_h(); self._touch_row(r)
    def _row_act_undo(self, r): self._ensure_row(r); self.items[r].original_state(); self._touch_row(r)

    def _ensure_row(self, r):
        if r < 0 or r >= len(self.items):
            raise IndexError("Ungültige Zeile")

    def _touch_row(self, r):
        status = "Originalzustand (geplant)" if self.items[r].restore_to_baseline else ("Bearbeitet" if self.items[r].is_edited() else "Original")
        self.table.item(r, 3).setText(status)
        if r == self.current_row:
            self.update_preview()

    # Header-Checkbox
    def on_header_clicked(self, section: int):
        if section != 0: return
        self.master_checked = not self.master_checked
        state = Qt.CheckState.Checked if self.master_checked else Qt.CheckState.Unchecked
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it: it.setCheckState(state)

    # >>> Einfache, stabile Massenaktionen (Top-Buttons) <<<
    def mass_apply(self, kind: str, value: Optional[float]):
        for i, item in enumerate(self.items):
            if kind == "rot":
                item.add_rotation(value or 0.0)
            elif kind == "mirror_h":
                item.mirror_h()
            self.table.item(i, 3).setText("Bearbeitet" if item.is_edited() else "Original")
        self.update_preview()

    def mass_original(self):
        for i, item in enumerate(self.items):
            item.original_state()
            self.table.item(i, 3).setText("Originalzustand (geplant)")
        self.update_preview()

    # Vorschau (immer Ist-Zustand der geplanten Bearbeitung)
    def update_preview(self):
        if self.current_row < 0 or self.current_row >= len(self.items):
            self.preview.setText("Vorschau")
            return
        item = self.items[self.current_row]
        try:
            if item.restore_to_baseline and item.baseline_bytes:
                im = Image.open(io.BytesIO(item.baseline_bytes))
            else:
                im = Image.open(item.path)
            im.load()
            im = ImageOps.exif_transpose(im)
            out = item.apply_to(im)
            out.thumbnail((900, 900), Image.Resampling.LANCZOS)
            buf = io.BytesIO(); out.save(buf, format="PNG")
            data = buf.getvalue(); buf.close()
            pix = QPixmap(); pix.loadFromData(data, "PNG")
            pix = pix.scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
            self.preview.setPixmap(pix)
            im.close(); out.close()
        except Exception as e:
            self.preview.setText(f"Fehler bei Vorschau: {e}")

    # Zielordner
    def choose_dest(self):
        d = QFileDialog.getExistingDirectory(self, "Zielordner wählen", "")
        if d: self.dest_dir = Path(d)

    # Speichern
    def start_save(self):
        if not self.items: return
        for r in range(self.table.rowCount()):
            self.items[r].active = (self.table.item(r, 0).checkState() == Qt.CheckState.Checked)
        self.worker = SaveWorker(self.items, self.chk_copy.isChecked(), self.dest_dir)
        self.worker.progress.connect(self.on_progress)
        self.worker.done.connect(self.on_done)
        self.worker.start()
        self.btn_start.setEnabled(False); self.btn_stop.setEnabled(True)

    def stop_save(self):
        if self.worker:
            self.worker.stop()
            self.btn_stop.setEnabled(False)

    def on_progress(self, row, msg):
        if 0 <= row < self.table.rowCount():
            self.table.item(row, 3).setText(msg)
        done = sum("Gespeichert" in self.table.item(i, 3).text() for i in range(self.table.rowCount()))
        self.progress.setValue(int(done / max(1, len(self.items)) * 100))

    def on_done(self):
        """Nach dem Speichern internen Zustand an die tatsächlichen Dateien anpassen."""
        # Jede Datei wurde bereits gespeichert, also den neuen Zustand als Baseline übernehmen
        for item in self.items:
            try:
                item.set_baseline_from_disk()  # neue Datei einlesen als "Original"
                item.reset_steps()  # Bearbeitungsschritte leeren
                item.restore_to_baseline = False
            except Exception:
                pass

        # GUI aktualisieren
        for r in range(self.table.rowCount()):
            self.table.item(r, 3).setText("Original")

        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress.setValue(100)
        self.update_preview()

    def keyPressEvent(self, event):
        key = event.key()
        modifiers = event.modifiers()

        # STRG+Z → Original Zustand
        if (modifiers & Qt.KeyboardModifier.ControlModifier) and key == Qt.Key.Key_Z:
            if 0 <= self.current_row < len(self.items):
                self.items[self.current_row].original_state()
                self._touch_row(self.current_row)
            return

        # ENTER → Start
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.btn_start.isEnabled():
                self.start_save()
            return

        # ↓
        if key == Qt.Key.Key_Down:
            rows = self.table.rowCount()
            if rows == 0:
                return
            if self.current_row == -1:
                self.table.selectRow(0)
            elif self.current_row < rows - 1:
                new_row = self.current_row + 1
                self.table.selectRow(new_row)
            else:
                # Ende erreicht → Ereignis an Tabelle weitergeben
                super().keyPressEvent(event)
            return

        # ↑
        if key == Qt.Key.Key_Up:
            rows = self.table.rowCount()
            if rows == 0:
                return
            if self.current_row == -1:
                self.table.selectRow(rows - 1)
            elif self.current_row > 0:
                new_row = self.current_row - 1
                self.table.selectRow(new_row)
            else:
                # Anfang erreicht → weiterreichen
                super().keyPressEvent(event)
            return

        # ENTF → löschen
        if key == Qt.Key.Key_Delete:
            self.delete_selected_rows()
            return

        super().keyPressEvent(event)

    def delete_selected_rows(self):
        rows = sorted([ix.row() for ix in self.table.selectionModel().selectedRows()], reverse=True)
        for r in rows:
            if 0 <= r < len(self.items):
                self.items.pop(r)
                self.table.removeRow(r)
        if self.table.rowCount() > 0:
            self.table.selectRow(min(rows[-1] if rows else 0, self.table.rowCount() - 1))
        else:
            self.current_row = -1
            self.preview.setText("Vorschau")

    def clear_all(self):
        self.table.setRowCount(0)
        self.items.clear()
        self.current_row = -1
        self.preview.setText("Vorschau")
        self.progress.setValue(0)

    # UI-Reset (Bearbeitungsschritte löschen, nicht Dateien anfassen)
    def reset_all(self):
        for it in self.items:
            it.reset_steps()
            it.restore_to_baseline = False
            it.active = True
        for r in range(self.table.rowCount()):
            self.table.item(r, 0).setCheckState(Qt.CheckState.Checked)
            self.table.item(r, 3).setText("Original")
        self.progress.setValue(0)
        if self.table.rowCount() > 0:
            self.table.selectRow(0)
        else:
            self.current_row = -1
            self.preview.setText("Vorschau")


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()