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
from fit_tool.base_type import BaseType
from fit_tool.field import Field
from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.profile_type import SubSport
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

# ── Tipi di immersione correggibili ──────────────────────────────────────────
# Il computer subacqueo può essere stato impostato erroneamente sul tipo di
# immersione sbagliato: questa mappa consente di forzare il valore corretto
# nel file unito. Il FIT SDK non prevede un sub_sport dedicato per la modalità
# CCR: viene registrata come multi_gas_diving e distinta tramite il campo
# dive_gas.mode = closed_circuit_diluent.
DIVE_GAS_MODE_FIELD_ID = 3  # id del campo 'mode' nel messaggio dive_gas (profilo FIT)
DIVE_GAS_MODE_OPEN_CIRCUIT = 0
DIVE_GAS_MODE_CLOSED_CIRCUIT_DILUENT = 1

DIVE_TYPES: dict[str, dict] = {
    "single_gas": {"sub_sport": SubSport.SINGLE_GAS_DIVING, "gas_mode": DIVE_GAS_MODE_OPEN_CIRCUIT},
    "multi_gas":  {"sub_sport": SubSport.MULTI_GAS_DIVING,  "gas_mode": DIVE_GAS_MODE_OPEN_CIRCUIT},
    "ccr":        {"sub_sport": SubSport.MULTI_GAS_DIVING,  "gas_mode": DIVE_GAS_MODE_CLOSED_CIRCUIT_DILUENT},
}
DEFAULT_DIVE_TYPE = "multi_gas"

# Per pre-selezionare in UI il tipo già impostato sul computer (sub_sport originale)
_SUB_SPORT_TO_DIVE_TYPE = {
    SubSport.SINGLE_GAS_DIVING.value: "single_gas",
    SubSport.MULTI_GAS_DIVING.value:  "multi_gas",
}


def detect_dive_type(sub_sport_raw) -> str:
    """Deduce il tipo di immersione dal sub_sport originale (per pre-selezione UI)."""
    return _SUB_SPORT_TO_DIVE_TYPE.get(sub_sport_raw, DEFAULT_DIVE_TYPE)

_DT_MIN_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)
_FIT_EPOCH_S = 631_065_600  # Unix seconds per il FIT epoch (1989-12-31 UTC)

# ── Traduzioni UI ─────────────────────────────────────────────────────────────
TRANSLATIONS: dict[str, dict[str, str]] = {
    "it": {
        "btn_add":            "➕  Aggiungi file…",
        "btn_remove":         "✖  Rimuovi",
        "btn_sort":           "⟳  Riordina per ora",
        "btn_merge":          "⚡  Unisci",
        "btn_browse":         "Sfoglia…",
        "lf_files":           "File selezionati",
        "lf_output":          "File di output",
        "lbl_dive_type":      "Tipo di immersione:",
        "dive_type_single_gas": "Gas Singolo",
        "dive_type_multi_gas":  "Multi Gas",
        "dive_type_ccr":        "CCR",
        "col_file":           "Nome file",
        "col_dive_n":         "Dive #",
        "col_datetime":       "Data/Ora (locale)",
        "col_max_depth":      "Prof. max",
        "col_bottom_time":    "Fondo",
        "col_duration":       "Durata",
        "status_initial":     "Aggiungi almeno 2 file .fit per iniziare.",
        "status_ready":       "{n} file pronti — premi Unisci per procedere.",
        "status_reading":     "Lettura {name}…",
        "status_merging":     "Merge di {n} immersioni in corso…",
        "status_done":        "✓ Merge completato → {name}",
        "status_error":       "✗ Errore: {exc}",
        "dlg_read_err_title": "Errore lettura file",
        "dlg_no_out_title":   "Percorso mancante",
        "dlg_no_out_msg":     "Specifica il percorso del file di output.",
        "dlg_done_title":     "Merge completato",
        "dlg_done_msg":       "File salvato con successo:\n{path}\n\n{n} immersioni unite.",
        "dlg_err_title":      "Errore durante il merge",
        "fd_open_title":      "Seleziona file .fit",
        "fd_open_ft_all":     "Tutti i file",
        "fd_save_title":      "Salva file merged",
    },
    "en": {
        "btn_add":            "➕  Add files…",
        "btn_remove":         "✖  Remove",
        "btn_sort":           "⟳  Sort by time",
        "btn_merge":          "⚡  Merge",
        "btn_browse":         "Browse…",
        "lf_files":           "Selected files",
        "lf_output":          "Output file",
        "lbl_dive_type":      "Dive type:",
        "dive_type_single_gas": "Single Gas",
        "dive_type_multi_gas":  "Multi Gas",
        "dive_type_ccr":        "CCR",
        "col_file":           "File name",
        "col_dive_n":         "Dive #",
        "col_datetime":       "Date/Time (local)",
        "col_max_depth":      "Max depth",
        "col_bottom_time":    "Bottom",
        "col_duration":       "Duration",
        "status_initial":     "Add at least 2 .fit files to start.",
        "status_ready":       "{n} files ready — click Merge to proceed.",
        "status_reading":     "Reading {name}…",
        "status_merging":     "Merging {n} dives…",
        "status_done":        "✓ Merge complete → {name}",
        "status_error":       "✗ Error: {exc}",
        "dlg_read_err_title": "File read error",
        "dlg_no_out_title":   "Missing path",
        "dlg_no_out_msg":     "Please specify the output file path.",
        "dlg_done_title":     "Merge complete",
        "dlg_done_msg":       "File saved successfully:\n{path}\n\n{n} dives merged.",
        "dlg_err_title":      "Merge error",
        "fd_open_title":      "Select .fit files",
        "fd_open_ft_all":     "All files",
        "fd_save_title":      "Save merged file",
    },
    "de": {
        "btn_add":            "➕  Dateien hinzufügen…",
        "btn_remove":         "✖  Entfernen",
        "btn_sort":           "⟳  Nach Zeit sortieren",
        "btn_merge":          "⚡  Zusammenführen",
        "btn_browse":         "Durchsuchen…",
        "lf_files":           "Ausgewählte Dateien",
        "lf_output":          "Ausgabedatei",
        "lbl_dive_type":      "Tauchgangstyp:",
        "dive_type_single_gas": "Einzelgas",
        "dive_type_multi_gas":  "Multigas",
        "dive_type_ccr":        "CCR",
        "col_file":           "Dateiname",
        "col_dive_n":         "Tauchgang #",
        "col_datetime":       "Datum/Uhrzeit (lokal)",
        "col_max_depth":      "Max. Tiefe",
        "col_bottom_time":    "Grundzeit",
        "col_duration":       "Dauer",
        "status_initial":     "Mindestens 2 .fit-Dateien hinzufügen.",
        "status_ready":       "{n} Dateien bereit — Zusammenführen klicken.",
        "status_reading":     "Lese {name}…",
        "status_merging":     "{n} Tauchgänge werden zusammengeführt…",
        "status_done":        "✓ Abgeschlossen → {name}",
        "status_error":       "✗ Fehler: {exc}",
        "dlg_read_err_title": "Fehler beim Lesen",
        "dlg_no_out_title":   "Pfad fehlt",
        "dlg_no_out_msg":     "Bitte Ausgabedateipfad angeben.",
        "dlg_done_title":     "Zusammenführen abgeschlossen",
        "dlg_done_msg":       "Datei erfolgreich gespeichert:\n{path}\n\n{n} Tauchgänge zusammengeführt.",
        "dlg_err_title":      "Fehler beim Zusammenführen",
        "fd_open_title":      ".fit-Dateien auswählen",
        "fd_open_ft_all":     "Alle Dateien",
        "fd_save_title":      "Zusammengeführte Datei speichern",
    },
    "fr": {
        "btn_add":            "➕  Ajouter des fichiers…",
        "btn_remove":         "✖  Supprimer",
        "btn_sort":           "⟳  Trier par heure",
        "btn_merge":          "⚡  Fusionner",
        "btn_browse":         "Parcourir…",
        "lf_files":           "Fichiers sélectionnés",
        "lf_output":          "Fichier de sortie",
        "lbl_dive_type":      "Type de plongée :",
        "dive_type_single_gas": "Gaz unique",
        "dive_type_multi_gas":  "Multi-gaz",
        "dive_type_ccr":        "CCR",
        "col_file":           "Nom du fichier",
        "col_dive_n":         "Plongée #",
        "col_datetime":       "Date/Heure (locale)",
        "col_max_depth":      "Prof. max",
        "col_bottom_time":    "Fond",
        "col_duration":       "Durée",
        "status_initial":     "Ajoutez au moins 2 fichiers .fit pour commencer.",
        "status_ready":       "{n} fichiers prêts — cliquez sur Fusionner.",
        "status_reading":     "Lecture de {name}…",
        "status_merging":     "Fusion de {n} plongées en cours…",
        "status_done":        "✓ Fusion terminée → {name}",
        "status_error":       "✗ Erreur : {exc}",
        "dlg_read_err_title": "Erreur de lecture",
        "dlg_no_out_title":   "Chemin manquant",
        "dlg_no_out_msg":     "Veuillez indiquer le chemin du fichier de sortie.",
        "dlg_done_title":     "Fusion terminée",
        "dlg_done_msg":       "Fichier enregistré avec succès :\n{path}\n\n{n} plongées fusionnées.",
        "dlg_err_title":      "Erreur lors de la fusion",
        "fd_open_title":      "Sélectionner des fichiers .fit",
        "fd_open_ft_all":     "Tous les fichiers",
        "fd_save_title":      "Enregistrer le fichier fusionné",
    },
    "es": {
        "btn_add":            "➕  Añadir archivos…",
        "btn_remove":         "✖  Eliminar",
        "btn_sort":           "⟳  Ordenar por hora",
        "btn_merge":          "⚡  Unir",
        "btn_browse":         "Explorar…",
        "lf_files":           "Archivos seleccionados",
        "lf_output":          "Archivo de salida",
        "lbl_dive_type":      "Tipo de inmersión:",
        "dive_type_single_gas": "Gas único",
        "dive_type_multi_gas":  "Multigás",
        "dive_type_ccr":        "CCR",
        "col_file":           "Nombre de archivo",
        "col_dive_n":         "Buceo #",
        "col_datetime":       "Fecha/Hora (local)",
        "col_max_depth":      "Prof. máx.",
        "col_bottom_time":    "Fondo",
        "col_duration":       "Duración",
        "status_initial":     "Añade al menos 2 archivos .fit para empezar.",
        "status_ready":       "{n} archivos listos — pulsa Unir para continuar.",
        "status_reading":     "Leyendo {name}…",
        "status_merging":     "Uniendo {n} inmersiones…",
        "status_done":        "✓ Unión completada → {name}",
        "status_error":       "✗ Error: {exc}",
        "dlg_read_err_title": "Error de lectura",
        "dlg_no_out_title":   "Ruta no especificada",
        "dlg_no_out_msg":     "Por favor, especifica la ruta del archivo de salida.",
        "dlg_done_title":     "Unión completada",
        "dlg_done_msg":       "Archivo guardado con éxito:\n{path}\n\n{n} inmersiones unidas.",
        "dlg_err_title":      "Error al unir",
        "fd_open_title":      "Seleccionar archivos .fit",
        "fd_open_ft_all":     "Todos los archivos",
        "fd_save_title":      "Guardar archivo unido",
    },
}

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
        self.sub_sport: int | None = None          # sub_sport raw originale (per pre-selezione tipo immersione)
        self.max_depth: float | None = None        # metri
        self.bottom_time: float | None = None      # secondi
        self.total_elapsed: float | None = None    # secondi
        self.global_header_frames: list = []       # preamble (solo dal file[0])
        self.session_frames: list = []             # frame della sessione
        self.activity_frame = None                 # frame activity (footer)
        self.last_record_fields: dict = {}         # campi ultimo record per gap filling
        self.session_meta: dict = {}               # metadati sessione per aggregazione
        self.session_dive_summary_frame = None     # dive_summary con reference_mesg=session

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
                    elif f.name == "sub_sport" and f.value is not None:
                        info.sub_sport = getattr(f, "raw_value", None)
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

            # Estrai metadati dai dive_summary
            if name == "dive_summary":
                ref_mesg = None
                for f in frame.fields:
                    if f.name == "reference_mesg":
                        ref_mesg = f.value
                        break
                # Cattura il frame sessione per N2/CNS aggregato
                if ref_mesg == "session" and info.session_dive_summary_frame is None:
                    info.session_dive_summary_frame = frame
                # Metadati numerici dal primo dive_summary con dive_number
                if info.dive_number is None:
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
MAX_GAP_FILL_RECORDS = 3600  # tetto per gap, a prescindere dalla durata (es. immersioni di giorni diversi)
MAX_REASONABLE_GAP_SECONDS = 4 * 3600  # oltre questa soglia il gap viene segnalato in log


def _fill_gap_records(
    builder: FitFileBuilder,
    gap_start: datetime,
    gap_end: datetime,
    last_fields: dict,
) -> int:
    """
    Inserisce record dummy di superficie nell'intervallo (gap_start, gap_end).
    Usa l'ultimo heart_rate/temperatura noti; depth=0 (superfice).
    Il passo tra un record e l'altro è di 1s, ma viene allargato se servirebbero
    più di MAX_GAP_FILL_RECORDS record (es. un gap di ore/giorni tra due file
    che non appartengono alla stessa sessione): senza questo limite generare
    un record al secondo per un gap del genere richiederebbe minuti e
    produrrebbe un file enorme.
    Restituisce il numero di record inseriti.
    """
    hr = last_fields.get("heart_rate")
    temp = last_fields.get("temperature")
    abs_pres = 101325  # Pa standard a superficie (1 atm)

    total_seconds = int((gap_end - gap_start).total_seconds())
    step_seconds = max(1, -(-total_seconds // MAX_GAP_FILL_RECORDS))  # ceil division

    t = gap_start + timedelta(seconds=step_seconds)
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
        t += timedelta(seconds=step_seconds)
        count += 1
    return count


def _build_merged_session(ordered_infos: list, dive_type: str = DEFAULT_DIVE_TYPE) -> SessionMessage:
    """
    Costruisce un singolo SessionMessage aggregato da tutte le immersioni.
    Usa il frame sessione del primo file come base (sport, sub_sport, ecc.)
    e sovrascrive con valori aggregati.
    dive_type sovrascrive il sub_sport per correggere un'eventuale impostazione
    errata sul computer subacqueo (vedi DIVE_TYPES).
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
    msg.total_timer_time = total_elapsed  # include gli intervalli di superficie (dummy)
    msg.total_calories = int(sum(float(m.get("total_calories") or 0) for m in metas))
    if start_time:
        msg.start_time = _to_fit_ms(start_time)
    if end_time:
        msg.timestamp = _to_fit_ms(end_time)
    msg.sub_sport = DIVE_TYPES[dive_type]["sub_sport"]

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


def _gas_key(frame: fitdecode.FitDataMessage) -> tuple:
    """Chiave di deduplicazione per un dive_gas (composizione del gas)."""
    o2 = he = None
    for f in frame.fields:
        if f.name == "oxygen_content" and f.value is not None:
            o2 = round(float(f.value), 3)
        elif f.name == "helium_content" and f.value is not None:
            he = round(float(f.value), 3)
    return (o2, he)


def _set_dive_gas_mode(msg: DiveGasMessage, mode: int) -> None:
    """
    Imposta il campo 'mode' (open_circuit/closed_circuit_diluent) su un dive_gas.
    fit-tool 0.9.15 non definisce ancora questo campo del profilo FIT: viene
    registrato dinamicamente sul messaggio così da essere comunque codificato
    nel file di output.
    """
    field = msg.get_field(DIVE_GAS_MODE_FIELD_ID)
    if field is None:
        field = Field(field_id=DIVE_GAS_MODE_FIELD_ID, name="mode", base_type=BaseType.ENUM, growable=True)
        msg.fields.append(field)
    field.set_value(0, mode)


def _build_merged_session_dive_summary(ordered_infos: list) -> DiveSummaryMessage:
    """
    Costruisce un unico DiveSummaryMessage di tipo 'session' che aggrega
    tutte le immersioni:
      - bottom_time = somma dei tempi attivi
      - max_depth = profondità massima effettiva su tutte le immersioni
      - start_n2/start_cns = dal dive_summary sessione del PRIMO file
      - end_n2/end_cns     = dal dive_summary sessione dell'ULTIMO file
    """
    first_ds = ordered_infos[0].session_dive_summary_frame
    last_ds  = ordered_infos[-1].session_dive_summary_frame

    msg = DiveSummaryMessage()
    # Base sul primo frame di sessione (preserva reference_mesg raw, dive_number ecc.)
    if first_ds is not None:
        _apply_fields(msg, first_ds)

    msg.reference_index = 0

    # Tempo totale attivo subacqueo
    total_bottom = sum(float(info.bottom_time or 0) for info in ordered_infos)
    msg.bottom_time = total_bottom

    # Profondità massima effettiva
    max_d = max(
        (info.max_depth for info in ordered_infos if info.max_depth is not None),
        default=None,
    )
    if max_d is not None:
        msg.max_depth = max_d

    # N2 e CNS all'uscita: usa l'ultimo file
    if last_ds is not None:
        for f in last_ds.fields:
            if f.name in {"end_n2", "end_cns"} and f.value is not None:
                try:
                    setattr(msg, f.name, f.value)
                except Exception:
                    try:
                        setattr(msg, f.name, getattr(f, "raw_value", f.value))
                    except Exception:
                        pass

    return msg


# ── Merger ────────────────────────────────────────────────────────────────────
def merge_fit_files(
    ordered_infos: list,
    output_path: Path,
    log_fn=None,
    dive_type: str = DEFAULT_DIVE_TYPE,
):
    """
    Unisce i file .fit in un'unica immersione (1 session, N lap) con record
    dummy di superficie tra un'immersione e l'altra.

    ordered_infos : lista di DiveFileInfo nell'ordine voluto dall'utente.
    output_path   : percorso del file .fit di output.
    log_fn        : callable(str) per messaggi di avanzamento (opzionale).
    dive_type     : "single_gas" | "multi_gas" | "ccr" — forza il tipo di
                    immersione nel file unito, per correggere un'eventuale
                    impostazione errata sul computer subacqueo.
    """

    def log(msg: str):
        if log_fn:
            log_fn(msg)

    if dive_type not in DIVE_TYPES:
        dive_type = DEFAULT_DIVE_TYPE

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

    # ── 2. Raccogli e deduplicazione dive_gas ─────────────────────────────
    seen_gas_keys: set = set()
    unique_gas_frames: list = []
    for info in ordered_infos:
        for frame in info.session_frames:
            if frame.name == "dive_gas":
                key = _gas_key(frame)
                if key not in seen_gas_keys:
                    seen_gas_keys.add(key)
                    unique_gas_frames.append(frame)
    for gas_idx, frame in enumerate(unique_gas_frames):
        gas_msg = _build_msg(frame, DiveGasMessage, {"message_index": gas_idx})
        _set_dive_gas_mode(gas_msg, DIVE_TYPES[dive_type]["gas_mode"])
        builder.add(gas_msg)
    log(f"  Tipi di gas unici emessi: {len(unique_gas_frames)}")
    log(f"  Tipo di immersione impostato: {dive_type}")

    # ── 3. Frame di ogni immersione + gap fill ─────────────────────────────
    for idx, info in enumerate(ordered_infos):
        dive_label = f"dive #{info.dive_number}" if info.dive_number else f"file {idx+1}"
        log(f"  Aggiunta immersione {idx + 1}/{n}: {dive_label}")

        for frame in info.session_frames:
            name = frame.name
            if name in {"session", "dive_gas"}:
                continue  # session → costruita dopo; dive_gas → già emessi deduplicati

            if name == "dive_summary":
                ref_mesg = None
                for f in frame.fields:
                    if f.name == "reference_mesg":
                        ref_mesg = f.value
                        break
                if ref_mesg == "session":
                    continue  # dive_summary sessione → sarà emessa aggregata dopo

            ft_class = KNOWN_MSG_CLASSES.get(name)
            if ft_class is None:
                log(f"  [ignorato] {name}")
                continue

            overrides: dict = {}
            if name == "lap":
                overrides["message_index"] = idx
            elif name == "dive_summary":
                overrides["reference_index"] = idx

            builder.add(_build_msg(frame, ft_class, overrides))

        # Gap fill verso l'immersione successiva
        if idx < n - 1:
            next_info = ordered_infos[idx + 1]
            gap_start = info.end_time
            gap_end = next_info.start_time
            if gap_start and gap_end and gap_end > gap_start:
                secs = int((gap_end - gap_start).total_seconds())
                if secs > MAX_REASONABLE_GAP_SECONDS:
                    log(
                        f"  ⚠ Gap {idx+1}→{idx+2} di {secs/3600:.1f}h: file forse di "
                        f"immersioni/giornate diverse, controlla la selezione."
                    )
                count = _fill_gap_records(builder, gap_start, gap_end, info.last_record_fields)
                log(f"  Gap {idx+1}→{idx+2}: {count} record superficie ({secs}s)")

    # ── 4. Sessione unica aggregata ────────────────────────────────────────
    log("  Costruzione sessione unica aggregata…")
    merged_session = _build_merged_session(ordered_infos, dive_type)
    builder.add(merged_session)

    # ── 4b. Dive summary aggregata a livello sessione ─────────────────────
    log("  Costruzione dive summary di sessione aggregata…")
    merged_ds = _build_merged_session_dive_summary(ordered_infos)
    builder.add(merged_ds)

    # ── 5. Activity footer ─────────────────────────────────────────────────
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
        self._lang = tk.StringVar(value="it")
        self._dive_type_keys = ["single_gas", "multi_gas", "ccr"]
        self._dive_type = tk.StringVar(value=DEFAULT_DIVE_TYPE)
        self._build_ui()

    # ── Costruzione UI ────────────────────────────────────────────────────
    def _build_ui(self):
        root = self.root

        # Toolbar
        toolbar = ttk.Frame(root, padding=(6, 6, 6, 2))
        toolbar.pack(fill=tk.X)

        self.btn_add = ttk.Button(toolbar, text="➕  Aggiungi file…", command=self._add_files)
        self.btn_add.pack(side=tk.LEFT, padx=2)
        self.btn_remove = ttk.Button(toolbar, text="✖  Rimuovi", command=self._remove_selected)
        self.btn_remove.pack(side=tk.LEFT, padx=2)
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(toolbar, text="↑", width=3, command=lambda: self._move(-1)).pack(side=tk.LEFT, padx=1)
        ttk.Button(toolbar, text="↓", width=3, command=lambda: self._move(+1)).pack(side=tk.LEFT, padx=1)
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        self.btn_sort = ttk.Button(toolbar, text="⟳  Riordina per ora", command=self._sort_by_time)
        self.btn_sort.pack(side=tk.LEFT, padx=2)

        # Selettore lingua (lato destro della toolbar)
        lang_cb = ttk.Combobox(
            toolbar, textvariable=self._lang, width=4, state="readonly",
            values=["it", "en", "de", "fr", "es"],
        )
        lang_cb.pack(side=tk.RIGHT, padx=(2, 4))
        ttk.Label(toolbar, text="🌐").pack(side=tk.RIGHT)
        self._lang.trace_add("write", self._apply_lang)

        # Treeview lista file
        self.lf_files = ttk.LabelFrame(root, text="File selezionati", padding=6)
        self.lf_files.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 4))
        frame_list = self.lf_files

        cols = ("file", "dive_n", "data_ora", "max_depth", "bottom_time", "durata")
        self.tree = ttk.Treeview(frame_list, columns=cols, show="headings", selectmode="browse")

        self.tree.heading("file",        text=self._t("col_file"))
        self.tree.heading("dive_n",      text=self._t("col_dive_n"))
        self.tree.heading("data_ora",    text=self._t("col_datetime"))
        self.tree.heading("max_depth",   text=self._t("col_max_depth"))
        self.tree.heading("bottom_time", text=self._t("col_bottom_time"))
        self.tree.heading("durata",      text=self._t("col_duration"))

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
        self.lf_output = ttk.LabelFrame(root, text="File di output", padding=6)
        self.lf_output.pack(fill=tk.X, padx=8, pady=(0, 4))
        frame_out = self.lf_output

        row_path = ttk.Frame(frame_out)
        row_path.pack(fill=tk.X)
        self.var_out = tk.StringVar()
        ttk.Entry(row_path, textvariable=self.var_out).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4)
        )
        self.btn_browse = ttk.Button(row_path, text="Sfoglia…", command=self._browse_output)
        self.btn_browse.pack(side=tk.LEFT)

        row_dive_type = ttk.Frame(frame_out)
        row_dive_type.pack(fill=tk.X, pady=(6, 0))
        self.lbl_dive_type = ttk.Label(row_dive_type, text=self._t("lbl_dive_type"))
        self.lbl_dive_type.pack(side=tk.LEFT, padx=(0, 6))
        self.cb_dive_type = ttk.Combobox(row_dive_type, state="readonly", width=20)
        self.cb_dive_type.pack(side=tk.LEFT)
        self.cb_dive_type.bind("<<ComboboxSelected>>", self._on_dive_type_selected)
        self._refresh_dive_type_combo()

        # Bottom bar: status + merge button
        frame_bot = ttk.Frame(root, padding=(8, 2, 8, 8))
        frame_bot.pack(fill=tk.X)

        self.btn_merge = ttk.Button(
            frame_bot, text="⚡  Unisci", command=self._merge, state=tk.DISABLED
        )
        self.btn_merge.pack(side=tk.RIGHT, padx=(6, 0))

        self.var_status = tk.StringVar(value=self._t("status_initial"))
        ttk.Label(frame_bot, textvariable=self.var_status, anchor=tk.W).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )

    # ── Internazionalizzazione ────────────────────────────────────────────
    def _t(self, key: str) -> str:
        """Restituisce la stringa tradotta per la lingua corrente."""
        return TRANSLATIONS.get(self._lang.get(), TRANSLATIONS["it"]).get(key, key)

    def _apply_lang(self, *_):
        """Aggiorna tutti i widget con le stringhe della lingua selezionata."""
        self.btn_add.config(text=self._t("btn_add"))
        self.btn_remove.config(text=self._t("btn_remove"))
        self.btn_sort.config(text=self._t("btn_sort"))
        self.btn_merge.config(text=self._t("btn_merge"))
        self.btn_browse.config(text=self._t("btn_browse"))
        self.lf_files.config(text=self._t("lf_files"))
        self.lf_output.config(text=self._t("lf_output"))
        self.lbl_dive_type.config(text=self._t("lbl_dive_type"))
        self._refresh_dive_type_combo()
        self.tree.heading("file",        text=self._t("col_file"))
        self.tree.heading("dive_n",      text=self._t("col_dive_n"))
        self.tree.heading("data_ora",    text=self._t("col_datetime"))
        self.tree.heading("max_depth",   text=self._t("col_max_depth"))
        self.tree.heading("bottom_time", text=self._t("col_bottom_time"))
        self.tree.heading("durata",      text=self._t("col_duration"))
        self._update_state()

    # ── Azioni toolbar ────────────────────────────────────────────────────
    def _add_files(self):
        paths = filedialog.askopenfilenames(
            title=self._t("fd_open_title"),
            filetypes=[("Garmin FIT", "*.fit"), (self._t("fd_open_ft_all"), "*.*")],
        )
        if not paths:
            return

        was_empty = not self._infos
        errors = []
        added = 0
        for p in paths:
            path = Path(p)
            if any(info.path == path for info in self._infos):
                continue  # già presente, salta
            try:
                self._set_status(self._t("status_reading").format(name=path.name))
                info = parse_fit_file(path)
                self._infos.append(info)
                self._append_tree_row(info)
                added += 1
            except Exception as exc:
                errors.append(f"{path.name}: {exc}")

        if errors:
            messagebox.showerror(
                self._t("dlg_read_err_title"),
                "\n".join(errors),
                parent=self.root,
            )

        if added:
            self._sort_by_time()
            self._refresh_output_name()
            if was_empty:
                # Pre-seleziona il tipo di immersione già impostato sul computer,
                # così il campo riflette lo stato reale finché l'utente non lo corregge.
                self._dive_type.set(detect_dive_type(self._infos[0].sub_sport))
                self._refresh_dive_type_combo()

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

    def _refresh_dive_type_combo(self):
        """Aggiorna le etichette del combobox tipo immersione nella lingua corrente."""
        self.cb_dive_type["values"] = [
            self._t(f"dive_type_{key}") for key in self._dive_type_keys
        ]
        idx = self._dive_type_keys.index(self._dive_type.get())
        self.cb_dive_type.current(idx)

    def _on_dive_type_selected(self, _event=None):
        idx = self.cb_dive_type.current()
        if idx >= 0:
            self._dive_type.set(self._dive_type_keys[idx])

    def _browse_output(self):
        initial = Path(self.var_out.get()) if self.var_out.get() else Path.home()
        path = filedialog.asksaveasfilename(
            title=self._t("fd_save_title"),
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
                self._t("dlg_no_out_title"),
                self._t("dlg_no_out_msg"),
                parent=self.root,
            )
            return

        output_path = Path(output)

        def log(msg: str):
            self._set_status(msg)
            self.root.update_idletasks()

        try:
            self.btn_merge["state"] = tk.DISABLED
            log(self._t("status_merging").format(n=len(self._infos)))
            merge_fit_files(
                self._infos, output_path, log_fn=log, dive_type=self._dive_type.get()
            )
            messagebox.showinfo(
                self._t("dlg_done_title"),
                self._t("dlg_done_msg").format(path=output_path, n=len(self._infos)),
                parent=self.root,
            )
            self._set_status(self._t("status_done").format(name=output_path.name))
        except Exception as exc:
            messagebox.showerror(
                self._t("dlg_err_title"),
                f"{exc}\n\nDettagli:\n{traceback.format_exc()}",
                parent=self.root,
            )
            self._set_status(self._t("status_error").format(exc=exc))
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
            self._set_status(self._t("status_ready").format(n=len(self._infos)))
        else:
            self._set_status(self._t("status_initial"))

    def _set_status(self, msg: str):
        self.var_status.set(msg)


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    root = tk.Tk()
    MergerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
