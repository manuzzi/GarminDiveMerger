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

print(f"\n  Sessions ({len(sessions)}):")
for s in sessions:
    print(
        f"    idx={s.get('message_index')}  num_laps={s.get('num_laps')}"
        f"  start={s.get('start_time')}  sport={s.get('sport')}"
        f"  elapsed={s.get('total_elapsed_time')}s  timer={s.get('total_timer_time')}s"
    )

print(f"\n  Laps ({len(laps)}):")
for lap in laps:
    print(f"    idx={lap.get('message_index')}  start={lap.get('start_time')}  elapsed={lap.get('total_elapsed_time')}s")

print(f"\n  DiveSummaries ({len(dive_summaries)}):")
for ds in dive_summaries:
    print(
        f"    ref={ds.get('reference_mesg')}  ref_idx={ds.get('reference_index')}"
        f"  max_depth={ds.get('max_depth')}  dive#={ds.get('dive_number')}"
    )

print(f"\n  Record totali: {len(records)}")
# Verifica gap: campiona alcuni record per mostrare timestamp e depth
gap_records = [r for r in records if r.get('depth', 1) == 0.0]
print(f"  Record superficie (depth=0): {len(gap_records)}")

# Asserzioni
assert len(sessions) == 1, f"Attesa 1 sessione, trovate {len(sessions)}"
assert sessions[0].get('message_index') == 0, "session.message_index != 0"
assert sessions[0].get('num_laps') == len(infos), f"num_laps atteso {len(infos)}, trovato {sessions[0].get('num_laps')}"
assert sessions[0].get('sport') == 'diving', f"sport atteso 'diving', trovato {sessions[0].get('sport')}"
assert len(laps) == len(infos), f"Attesi {len(infos)} lap, trovati {len(laps)}"
for i, lap in enumerate(laps):
    assert lap.get('message_index') == i, f"lap[{i}].message_index != {i}"
print(f"\nTutte le asserzioni passate. File: {OUT}  ({OUT.stat().st_size / 1024:.1f} KB)")
