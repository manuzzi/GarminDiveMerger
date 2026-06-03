from pathlib import Path
from merge_fit import parse_fit_file, merge_fit_files

TEMP = Path('temp')
OUT = TEMP / "test_merged.fit"
# Prendi solo i file originali ACTIVITY (esclude file merged precedenti)
files = sorted(TEMP.glob('*_ACTIVITY.fit'))
print(f"File trovati: {len(files)}")

infos = []
for f in files:
    info = parse_fit_file(f)
    print(
        f"  {f.name}: dive#{info.dive_number}  {info.local_start_time}"
        f"  max={info.max_depth}m  fondo={int(info.bottom_time)}s"
        f"  end={info.end_time}  last_hr={info.last_record_fields.get('heart_rate')}"
    )
    infos.append(info)

print(f"\nMerge di {len(infos)} file...")
if OUT.exists():
    OUT.unlink()
merge_fit_files(infos, OUT, log_fn=print)

print("\nVerifica file merged:")
import fitdecode
msg_counts = {}
sessions = []
laps = []
dive_summaries = []
records = []
with fitdecode.FitReader(str(OUT)) as fit:
    for frame in fit:
        if not isinstance(frame, fitdecode.FitDataMessage):
            continue
        msg_counts[frame.name] = msg_counts.get(frame.name, 0) + 1
        if frame.name == 'session':
            d = {f.name: f.value for f in frame.fields if f.value is not None}
            sessions.append(d)
        if frame.name == 'lap':
            d = {f.name: f.value for f in frame.fields if f.value is not None}
            laps.append(d)
        if frame.name == 'activity':
            d = {f.name: f.value for f in frame.fields if f.value is not None}
            print(f"  activity: num_sessions={d.get('num_sessions')}  type={d.get('type')}")
        if frame.name == 'dive_summary':
            d = {f.name: f.value for f in frame.fields if f.value is not None}
            dive_summaries.append(d)
        if frame.name == 'record':
            d = {f.name: f.value for f in frame.fields if f.value is not None}
            records.append(d)

print(f"  Message counts: {msg_counts}")

print(f"\n  Sessions (1 attesa):")
for s in sessions:
    print(
        f"    idx={s.get('message_index')}  num_laps={s.get('num_laps')}"
        f"  start={s.get('start_time')}  sport={s.get('sport')}"
        f"  elapsed={s.get('total_elapsed_time')}s  timer={s.get('total_timer_time')}s"
        f"  cal={s.get('total_calories')}"
    )

print(f"\n  Laps ({len(laps)}):")
for lap in laps:
    print(f"    idx={lap.get('message_index')}  start={lap.get('start_time')}  elapsed={lap.get('total_elapsed_time')}s")

print(f"\n  DiveSummaries session-ref (1 attesa):")
session_ds = [ds for ds in dive_summaries if ds.get('reference_mesg') == 'session']
lap_ds     = [ds for ds in dive_summaries if ds.get('reference_mesg') != 'session']
for ds in session_ds:
    print(
        f"    ref={ds.get('reference_mesg')}  ref_idx={ds.get('reference_index')}"
        f"  max_depth={ds.get('max_depth')}  bottom_time={ds.get('bottom_time')}"
        f"  dive#={ds.get('dive_number')}"
        f"  start_n2={ds.get('start_n2')}  end_n2={ds.get('end_n2')}"
        f"  start_cns={ds.get('start_cns')}  end_cns={ds.get('end_cns')}"
    )
print(f"\n  DiveSummaries lap-ref ({len(lap_ds)}, attesi {len(infos)}):")
for ds in lap_ds:
    print(
        f"    ref={ds.get('reference_mesg')}  ref_idx={ds.get('reference_index')}"
        f"  max_depth={ds.get('max_depth')}  dive#={ds.get('dive_number')}"
    )

print(f"\n  Record totali: {len(records)}")
gap_records = [r for r in records if r.get('depth', 1.0) == 0.0]
print(f"  Record superficie (depth=0): {len(gap_records)}")

# Asserzioni
assert len(sessions) == 1, f"Attesa 1 sessione, trovate {len(sessions)}"
assert sessions[0].get('message_index') == 0
assert sessions[0].get('num_laps') == len(infos), f"num_laps atteso {len(infos)}"
assert sessions[0].get('total_elapsed_time') == sessions[0].get('total_timer_time'), \
    "total_elapsed_time != total_timer_time"
assert sessions[0].get('total_calories', 0) > 0, "calorie = 0"
assert len(laps) == len(infos), f"Attesi {len(infos)} lap, trovati {len(laps)}"
assert len(session_ds) == 1, f"Attesa 1 session dive_summary, trovate {len(session_ds)}"
assert len(lap_ds) == len(infos), f"Attese {len(infos)} lap dive_summary"
# max_depth nella dive_summary sessione deve essere la globale
expected_max = max(i.max_depth for i in infos if i.max_depth is not None)
assert session_ds[0].get('max_depth') == expected_max, \
    f"max_depth atteso {expected_max}, trovato {session_ds[0].get('max_depth')}"
# Verifica gas unici
import fitdecode as _fd
gas_msgs = []
with _fd.FitReader(str(OUT)) as _fit:
    for _frame in _fit:
        if isinstance(_frame, _fd.FitDataMessage) and _frame.name == 'dive_gas':
            gas_msgs.append({f.name: f.value for f in _frame.fields if f.value is not None})
print(f"\n  Gas nel file merged ({len(gas_msgs)}):")
for g in gas_msgs:
    print(f"    {g}")
assert len(gas_msgs) == 1, f"Atteso 1 gas, trovati {len(gas_msgs)}"
print(f"\nTutte le asserzioni passate. File: {OUT}  ({OUT.stat().st_size / 1024:.1f} KB)")
