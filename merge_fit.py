#!/usr/bin/env python3
"""
GarminDiveMerger — Unisce file .fit di immersioni Garmin (Descent MK3) in un
unico file a sessione singola importabile su Garmin Connect.
"""

# ── Imports ───────────────────────────────────────────────────────────────────
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from datetime import datetime, timezone, timedelta
import traceback

import fitdecode
from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.device_info_message import DeviceInfoMessage
from fit_tool.profile.messages.dive_summary_message import DiveSummaryMessage
from fit_tool.profile.messages.dive_settings_message import DiveSettingsMessage
from fit_tool.profile.messages.dive_gas_message import DiveGasMessage


# ── Costanti ──────────────────────────────────────────────────────────────────
# Messaggi del preamble globale: tenuti solo dal primo file nel merge
GLOBAL_HEADER_NAMES = {
    "file_id", "file_creator", "device_settings", "user_profile",
    "zones_target", "training_settings", "sport", "device_info",
    "timestamp_correlation",
}

# Messaggi ripetuti per ogni sessione (da ogni file sorgente)
SESSION_MSG_NAMES = {
    "event", "record", "lap", "time_in_zone",
    "dive_settings", "dive_gas", "dive_alarm",
    "dive_summary", "session",
}

# Mapping nome fitdecode → classe fit-tool
KNOWN_MSG_CLASSES: dict = {
    "file_id":       FileIdMessage,
    "activity":      ActivityMessage,
    "session":       SessionMessage,
    "record":        RecordMessage,
    "lap":           LapMessage,
    "event":         EventMessage,
    "device_info":   DeviceInfoMessage,
    "dive_summary":  DiveSummaryMessage,
    "dive_settings": DiveSettingsMessage,
    "dive_gas":      DiveGasMessage,
}

# Campi che portano un timestamp e vanno convertiti in ms dall'epoch FIT
TIMESTAMP_FIELD_NAMES = {"timestamp", "time_created", "local_timestamp", "start_time"}

_DT_MIN_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)
_FIT_EPOCH_S = 631_065_600  # Unix seconds per il FIT epoch (1989-12-31 UTC)


# ── Utilità di conversione ────────────────────────────────────────────────────
def _to_fit_ms(value) -> int | None:
    """Converte un datetime in millisecondi dall'epoch Unix (richiesto da fit-tool)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1000)
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _is_all_none(value) -> bool:
    """True se value è una tupla/lista con tutti elementi None."""
    return isinstance(value, (tuple, list)) and all(v is None for v in value)


def _apply_fields(msg, frame: fitdecode.FitDataMessage, overrides: dict | None = None):
    """
    Copia i campi di un frame fitdecode nell'oggetto fit-tool msg.
    I campi timestamp vengono convertiti in ms FIT.
    Gli overrides sovrascrivono i valori del frame dopo la copia.
    """
    for field in frame.fields:
        name = field.name
        value = field.value
        if value is None or _is_all_none(value):
            continue
        if name in TIMESTAMP_FIELD_NAMES:
            value = _to_fit_ms(value)
        try:
            setattr(msg, name, value)
        except Exception:
            # Fallback: prova il raw_value intero (per campi enum che fit-tool non accetta come stringa)
            try:
                raw = getattr(field, 'raw_value', None)
                if raw is not None and raw != value:
                    setattr(msg, name, raw)
            except Exception:
                pass

    if overrides:
        for name, value in overrides.items():
            if name in TIMESTAMP_FIELD_NAMES:
                value = _to_fit_ms(value)
            try:
                setattr(msg, name, value)
            except Exception:
                pass
    return msg


def _build_msg(frame: fitdecode.FitDataMessage, ft_class, overrides: dict | None = None):
    """Crea e popola un oggetto fit-tool dalla classe indicata."""
    return _apply_fields(ft_class(), frame, overrides)


# ── Modello dati ──────────────────────────────────────────────────────────────
class DiveFileInfo:
    """Metadati e frame parsati di un singolo file .fit di immersione."""

    def __init__(self):
        self.path: Path = None
        self.start_time: datetime | None = None   # UTC
        self.end_time: datetime | None = None     # UTC timestamp ultimo record
        self.local_offset_h: int = 0               # offset ora locale rispetto UTC
        self.dive_number: int | None = None
        self.max_depth: float | None = None        # metri
        self.bottom_time: float | None = None      # secondi
        self.total_elapsed: float | None = None    # secondi
        self.global_header_frames: list = []       # preamble (solo dal file[0])
        self.session_frames: list = []             # frame della sessione
        self.activity_frame = None                 # frame activity (footer)
        self.last_record_fields: dict = {}         # campi ultimo record per gap filling
        self.session_meta: dict = {}               # metadati sessione per aggregazione

    @property
    def local_start_time(self) -> datetime | None:
        if self.start_time is None:
            return None
        return self.start_time + timedelta(hours=self.local_offset_h)


# ── Parser ────────────────────────────────────────────────────────────────────
def parse_fit_file(path: Path) -> DiveFileInfo:
    """
    Legge un file .fit e restituisce un DiveFileInfo popolato.
    Lancia eccezione se il file non è valido o non contiene immersioni.
    """
    info = DiveFileInfo()
    info.path = path
    info.global_header_frames = []
    info.session_frames = []

    with fitdecode.FitReader(str(path)) as fit:
        for frame in fit:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue

            name = frame.name

            if name == "activity":
                info.activity_frame = frame
                # Calcola offset UTC dall'activity (timestamp vs local_timestamp)
                ts = None
                local_ts = None
                for f in frame.fields:
                    if f.name == "timestamp" and f.value is not None:
                        ts = f.value
                    elif f.name == "local_timestamp" and f.value is not None:
                        local_ts = f.value
                if ts is not None and local_ts is not None:
                    try:
                        delta = local_ts - ts
                        info.local_offset_h = int(round(delta.total_seconds() / 3600))
                    except Exception:
                        pass

            elif name in GLOBAL_HEADER_NAMES:
                info.global_header_frames.append(frame)

            else:
                info.session_frames.append(frame)

            # Estrai metadati dalla session
            if name == "session":
                meta: dict = {}
                for f in frame.fields:
                    if f.name == "start_time" and f.value is not None:
                        info.start_time = f.value
                    elif f.name == "total_elapsed_time" and f.value is not None:
                        info.total_elapsed = float(f.value)
                    if f.name in {
                        "total_timer_time", "total_calories",
                        "avg_heart_rate", "max_heart_rate", "min_heart_rate",
                        "avg_temperature", "max_temperature", "min_temperature",
                        "total_ascent", "total_descent",
                    } and f.value is not None:
                        meta[f.name] = f.value
                info.session_meta = meta

            # Traccia l'ultimo record per end_time e gap filling
            if name == "record":
                rec_ts = None
                rec_fields: dict = {}
                for f in frame.fields:
                    if f.name == "timestamp" and f.value is not None:
                        rec_ts = f.value
                    elif f.name != "timestamp" and f.value is not None and not _is_all_none(f.value):
                        rec_fields[f.name] = f.value
                if rec_ts is not None:
                    info.end_time = rec_ts
                    info.last_record_fields = rec_fields

            # Estrai metadati dal primo dive_summary con dive_number
            if name == "dive_summary" and info.dive_number is None:
                for f in frame.fields:
                    if f.name == "dive_number" and f.value is not None:
                        info.dive_number = int(f.value)
                    elif f.name == "max_depth" and f.value is not None:
                        info.max_depth = float(f.value)
                    elif f.name == "bottom_time" and f.value is not None:
                        info.bottom_time = float(f.value)

    if info.start_time is None:
        raise ValueError("Nessun messaggio 'session' trovato nel file.")

    return info


# ── Helpers merge ────────────────────────────────────────────────────────────
def _fill_gap_records(
    builder: FitFileBuilder,
    gap_start: datetime,
    gap_end: datetime,
    last_fields: dict,
) -> int:
    """
    Inserisce record dummy di superficie nell'intervallo (gap_start, gap_end).
    Usa l'ultimo heart_rate/temperatura noti; depth=0 (superfice).
    Restituisce il numero di record inseriti.
    """
    hr = last_fields.get("heart_rate")
    temp = last_fields.get("temperature")
    abs_pres = 101325  # Pa standard a superficie (1 atm)
    t = gap_start + timedelta(seconds=1)
    count = 0
    while t < gap_end:
        rec = RecordMessage()
        rec.timestamp = _to_fit_ms(t)
        rec.depth = 0.0
        rec.absolute_pressure = abs_pres
        if hr is not None:
            try:
                rec.heart_rate = int(hr)
            except Exception:
                pass
        if temp is not None:
            try:
                rec.temperature = int(temp)
            except Exception:
                pass
        builder.add(rec)
        t += timedelta(seconds=1)
        count += 1
    return count


def _build_merged_session(ordered_infos: list) -> SessionMessage:
    """
    Costruisce un singolo SessionMessage aggregato da tutte le immersioni.
    Usa il frame sessione del primo file come base (sport, sub_sport, ecc.)
    e sovrascrive con valori aggregati.
    """
    first = ordered_infos[0]
    last = ordered_infos[-1]
    n = len(ordered_infos)

    start_time = first.start_time
    end_time = last.end_time or last.start_time
    total_elapsed = (
        (end_time - start_time).total_seconds()
        if start_time and end_time
        else 0.0
    )
    metas = [info.session_meta for info in ordered_infos]

    # Usa il frame sessione del primo file come base (preserva sport/sub_sport ecc.)
    base_frame = next(
        (f for f in first.session_frames if f.name == "session"), None
    )
    msg = SessionMessage()
    if base_frame is not None:
        _apply_fields(msg, base_frame)

    # Sovrascritture aggregate
    msg.message_index = 0
    msg.first_lap_index = 0
    msg.num_laps = n
    msg.total_elapsed_time = total_elapsed
    msg.total_timer_time = sum(float(m.get("total_timer_time") or 0) for m in metas)
    msg.total_calories = int(sum(float(m.get("total_calories") or 0) for m in metas))
    if start_time:
        msg.start_time = _to_fit_ms(start_time)
    if end_time:
        msg.timestamp = _to_fit_ms(end_time)

    avg_hrs = [m["avg_heart_rate"] for m in metas if m.get("avg_heart_rate") is not None]
    if avg_hrs:
        msg.avg_heart_rate = int(sum(avg_hrs) / len(avg_hrs))
    max_hrs = [m["max_heart_rate"] for m in metas if m.get("max_heart_rate") is not None]
    if max_hrs:
        msg.max_heart_rate = max(max_hrs)
    min_hrs = [m["min_heart_rate"] for m in metas if m.get("min_heart_rate") is not None]
    if min_hrs:
        msg.min_heart_rate = min(min_hrs)

    avg_temps = [m["avg_temperature"] for m in metas if m.get("avg_temperature") is not None]
    if avg_temps:
        msg.avg_temperature = int(sum(avg_temps) / len(avg_temps))
    max_temps = [m["max_temperature"] for m in metas if m.get("max_temperature") is not None]
    if max_temps:
        msg.max_temperature = max(max_temps)
    min_temps = [m["min_temperature"] for m in metas if m.get("min_temperature") is not None]
    if min_temps:
        msg.min_temperature = min(min_temps)

    total_ascent = sum(float(m.get("total_ascent") or 0) for m in metas)
    total_descent = sum(float(m.get("total_descent") or 0) for m in metas)
    msg.total_ascent = total_ascent
    msg.total_descent = total_descent

    return msg


# ── Merger ────────────────────────────────────────────────────────────────────
def merge_fit_files(
    ordered_infos: list,
    output_path: Path,
    log_fn=None,
):
    """
    Unisce i file .fit in un'unica immersione (1 session, N lap) con record
    dummy di superficie tra un'immersione e l'altra.

    ordered_infos : lista di DiveFileInfo nell'ordine voluto dall'utente.
    output_path   : percorso del file .fit di output.
    log_fn        : callable(str) per messaggi di avanzamento (opzionale).
    """

    def log(msg: str):
        if log_fn:
            log_fn(msg)

    n = len(ordered_infos)
    builder = FitFileBuilder(auto_define=True)

    time_created = min(
        (info.start_time for info in ordered_infos if info.start_time),
        default=None,
    )

    # ── 1. Preamble globale (solo dal primo file) ──────────────────────────
    first = ordered_infos[0]
    for frame in first.global_header_frames:
        ft_class = KNOWN_MSG_CLASSES.get(frame.name)
        if ft_class is None:
            log(f"  [preamble ignorato] {frame.name}")
            continue
        overrides = {}
        if frame.name == "file_id" and time_created is not None:
            overrides["time_created"] = time_created
        builder.add(_build_msg(frame, ft_class, overrides))

    # ── 2. Frame di ogni immersione + gap fill ─────────────────────────────
    for idx, info in enumerate(ordered_infos):
        dive_label = f"dive #{info.dive_number}" if info.dive_number else f"file {idx+1}"
        log(f"  Aggiunta immersione {idx + 1}/{n}: {dive_label}")

        for frame in info.session_frames:
            name = frame.name
            if name == "session":
                continue  # la sessione unica è costruita in seguito

            ft_class = KNOWN_MSG_CLASSES.get(name)
            if ft_class is None:
                log(f"  [ignorato] {name}")
                continue

            overrides: dict = {}
            if name == "lap":
                overrides["message_index"] = idx
            elif name == "dive_summary":
                ref_mesg = None
                for f in frame.fields:
                    if f.name == "reference_mesg":
                        ref_mesg = f.value  # 'session' oppure 'lap'
                        break
                if ref_mesg == "session":
                    overrides["reference_index"] = 0  # unica sessione unificata
                else:
                    overrides["reference_index"] = idx

            builder.add(_build_msg(frame, ft_class, overrides))

        # Gap fill verso l'immersione successiva
        if idx < n - 1:
            next_info = ordered_infos[idx + 1]
            gap_start = info.end_time
            gap_end = next_info.start_time
            if gap_start and gap_end and gap_end > gap_start:
                secs = int((gap_end - gap_start).total_seconds())
                count = _fill_gap_records(builder, gap_start, gap_end, info.last_record_fields)
                log(f"  Gap {idx+1}→{idx+2}: {count} record superficie ({secs}s)")

    # ── 3. Sessione unica aggregata ────────────────────────────────────────
    log("  Costruzione sessione unica aggregata…")
    merged_session = _build_merged_session(ordered_infos)
    builder.add(merged_session)

    # ── 4. Activity footer ─────────────────────────────────────────────────
    last = ordered_infos[-1]
    activity_frame = first.activity_frame
    if activity_frame is not None:
        act = ActivityMessage()
        _apply_fields(act, activity_frame)
    else:
        act = ActivityMessage()
        if time_created is not None:
            act.timestamp = _to_fit_ms(time_created)
    act.num_sessions = 1
    if last.end_time is not None:
        act.timestamp = _to_fit_ms(last.end_time)
    builder.add(act)

    # ── Scrivi il file ─────────────────────────────────────────────────────
    fit_file = builder.build()
    fit_file.to_file(str(output_path))
    size_kb = output_path.stat().st_size / 1024
    log(f"  Salvato: {output_path.name}  ({size_kb:.1f} KB)")


# ── GUI ───────────────────────────────────────────────────────────────────────
class MergerApp:
    """Interfaccia grafica tkinter per GarminDiveMerger."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("GarminDiveMerger")
        self.root.resizable(True, True)
        self.root.minsize(740, 440)

        self._infos: list[DiveFileInfo] = []
        self._build_ui()

    # ── Costruzione UI ────────────────────────────────────────────────────
    def _build_ui(self):
        root = self.root

        # Toolbar
        toolbar = ttk.Frame(root, padding=(6, 6, 6, 2))
        toolbar.pack(fill=tk.X)

        ttk.Button(toolbar, text="➕  Aggiungi file…", command=self._add_files).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="✖  Rimuovi",         command=self._remove_selected).pack(side=tk.LEFT, padx=2)
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(toolbar, text="↑", width=3, command=lambda: self._move(-1)).pack(side=tk.LEFT, padx=1)
        ttk.Button(toolbar, text="↓", width=3, command=lambda: self._move(+1)).pack(side=tk.LEFT, padx=1)
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(toolbar, text="⟳  Riordina per ora", command=self._sort_by_time).pack(side=tk.LEFT, padx=2)

        # Treeview lista file
        frame_list = ttk.LabelFrame(root, text="File selezionati", padding=6)
        frame_list.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 4))

        cols = ("file", "dive_n", "data_ora", "max_depth", "bottom_time", "durata")
        self.tree = ttk.Treeview(frame_list, columns=cols, show="headings", selectmode="browse")

        self.tree.heading("file",        text="Nome file")
        self.tree.heading("dive_n",      text="Dive #")
        self.tree.heading("data_ora",    text="Data/Ora (locale)")
        self.tree.heading("max_depth",   text="Prof. max")
        self.tree.heading("bottom_time", text="Fondo")
        self.tree.heading("durata",      text="Durata")

        self.tree.column("file",        width=200, minwidth=120, anchor=tk.W)
        self.tree.column("dive_n",      width=60,  minwidth=50,  anchor=tk.CENTER)
        self.tree.column("data_ora",    width=165, minwidth=140, anchor=tk.CENTER)
        self.tree.column("max_depth",   width=90,  minwidth=70,  anchor=tk.CENTER)
        self.tree.column("bottom_time", width=80,  minwidth=60,  anchor=tk.CENTER)
        self.tree.column("durata",      width=90,  minwidth=70,  anchor=tk.CENTER)

        sb = ttk.Scrollbar(frame_list, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        # Output path
        frame_out = ttk.LabelFrame(root, text="File di output", padding=6)
        frame_out.pack(fill=tk.X, padx=8, pady=(0, 4))

        self.var_out = tk.StringVar()
        ttk.Entry(frame_out, textvariable=self.var_out).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4)
        )
        ttk.Button(frame_out, text="Sfoglia…", command=self._browse_output).pack(side=tk.LEFT)

        # Bottom bar: status + merge button
        frame_bot = ttk.Frame(root, padding=(8, 2, 8, 8))
        frame_bot.pack(fill=tk.X)

        self.btn_merge = ttk.Button(
            frame_bot, text="⚡  Unisci", command=self._merge, state=tk.DISABLED
        )
        self.btn_merge.pack(side=tk.RIGHT, padx=(6, 0))

        self.var_status = tk.StringVar(value="Aggiungi almeno 2 file .fit per iniziare.")
        ttk.Label(frame_bot, textvariable=self.var_status, anchor=tk.W).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )

    # ── Azioni toolbar ────────────────────────────────────────────────────
    def _add_files(self):
        paths = filedialog.askopenfilenames(
            title="Seleziona file .fit",
            filetypes=[("Garmin FIT", "*.fit"), ("Tutti i file", "*.*")],
        )
        if not paths:
            return

        errors = []
        added = 0
        for p in paths:
            path = Path(p)
            if any(info.path == path for info in self._infos):
                continue  # già presente, salta
            try:
                self._set_status(f"Lettura {path.name}…")
                info = parse_fit_file(path)
                self._infos.append(info)
                self._append_tree_row(info)
                added += 1
            except Exception as exc:
                errors.append(f"{path.name}: {exc}")

        if errors:
            messagebox.showerror(
                "Errore lettura file",
                "\n".join(errors),
                parent=self.root,
            )

        if added:
            self._sort_by_time()
            self._refresh_output_name()

        self._update_state()

    def _remove_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        self._infos = [i for i in self._infos if str(id(i)) != iid]
        self.tree.delete(iid)
        self._refresh_output_name()
        self._update_state()

    def _move(self, direction: int):
        """Sposta la riga selezionata di una posizione su (−1) o giù (+1)."""
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        idx = next(
            (i for i, info in enumerate(self._infos) if str(id(info)) == iid), None
        )
        if idx is None:
            return
        new_idx = idx + direction
        if new_idx < 0 or new_idx >= len(self._infos):
            return
        self._infos[idx], self._infos[new_idx] = self._infos[new_idx], self._infos[idx]
        self._rebuild_tree()
        self.tree.selection_set(str(id(self._infos[new_idx])))

    def _sort_by_time(self):
        """Riordina i file per start_time crescente."""
        self._infos.sort(key=lambda i: i.start_time or _DT_MIN_UTC)
        self._rebuild_tree()
        self._update_state()

    def _browse_output(self):
        initial = Path(self.var_out.get()) if self.var_out.get() else Path.home()
        path = filedialog.asksaveasfilename(
            title="Salva file merged",
            defaultextension=".fit",
            filetypes=[("Garmin FIT", "*.fit")],
            initialdir=str(initial.parent) if initial.suffix else str(initial),
            initialfile=initial.name if initial.suffix else "merged.fit",
            parent=self.root,
        )
        if path:
            self.var_out.set(path)

    def _merge(self):
        output = self.var_out.get().strip()
        if not output:
            messagebox.showwarning(
                "Percorso mancante",
                "Specifica il percorso del file di output.",
                parent=self.root,
            )
            return

        output_path = Path(output)

        def log(msg: str):
            self._set_status(msg)
            self.root.update_idletasks()

        try:
            self.btn_merge["state"] = tk.DISABLED
            log(f"Merge di {len(self._infos)} immersioni in corso…")
            merge_fit_files(self._infos, output_path, log_fn=log)
            messagebox.showinfo(
                "Merge completato",
                f"File salvato con successo:\n{output_path}\n\n"
                f"{len(self._infos)} immersioni unite.",
                parent=self.root,
            )
            self._set_status(f"✓ Merge completato → {output_path.name}")
        except Exception as exc:
            messagebox.showerror(
                "Errore durante il merge",
                f"{exc}\n\nDettagli:\n{traceback.format_exc()}",
                parent=self.root,
            )
            self._set_status(f"✗ Errore: {exc}")
        finally:
            self._update_state()

    # ── Helpers UI ────────────────────────────────────────────────────────
    def _append_tree_row(self, info: DiveFileInfo):
        local_dt = info.local_start_time
        dt_str   = local_dt.strftime("%d/%m/%Y  %H:%M:%S") if local_dt else "—"
        dive_n   = str(info.dive_number)  if info.dive_number  is not None else "—"
        depth    = f"{info.max_depth:.1f} m"  if info.max_depth   is not None else "—"
        fondo    = f"{int(info.bottom_time)}s" if info.bottom_time is not None else "—"
        durata   = f"{int(info.total_elapsed)}s" if info.total_elapsed is not None else "—"
        self.tree.insert(
            "", tk.END, iid=str(id(info)),
            values=(info.path.name, dive_n, dt_str, depth, fondo, durata),
        )

    def _rebuild_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for info in self._infos:
            self._append_tree_row(info)

    def _refresh_output_name(self):
        if not self._infos:
            self.var_out.set("")
            return
        sorted_infos = sorted(
            self._infos, key=lambda i: i.start_time or _DT_MIN_UTC
        )
        dive_nums = [i.dive_number for i in sorted_infos if i.dive_number is not None]
        date_str = (
            sorted_infos[0].start_time.strftime("%Y%m%d")
            if sorted_infos[0].start_time
            else "unknown"
        )
        if dive_nums:
            filename = f"merged_dive{min(dive_nums)}-{max(dive_nums)}_{date_str}.fit"
        else:
            filename = f"merged_{date_str}.fit"
        out_dir = sorted_infos[0].path.parent
        self.var_out.set(str(out_dir / filename))

    def _update_state(self):
        can_merge = len(self._infos) >= 2
        self.btn_merge["state"] = tk.NORMAL if can_merge else tk.DISABLED
        if can_merge:
            self._set_status(
                f"{len(self._infos)} file pronti — premi Unisci per procedere."
            )
        else:
            self._set_status("Aggiungi almeno 2 file .fit per iniziare.")

    def _set_status(self, msg: str):
        self.var_status.set(msg)


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    root = tk.Tk()
    MergerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
