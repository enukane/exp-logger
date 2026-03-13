# Data Format Requirements

This document describes the requirements for placing custom data under `data/`.

## Directory Structure

```
data/
  <name>/
    <any_filename>
```

- Each subdirectory under `data/` is treated as a **data source**.
- The directory name becomes the **data key** in the `/status` API response (e.g., `data/sensor/` -> key `"sensor"`).

## File Format

- Files must be **JSONL** (JSON Lines): one JSON object per line.
- The last non-empty line of the most recently modified file is read as the latest data point.

### File Selection Priority

1. If files matching `*_pos.log` exist in the directory, only those files are considered (GPS-specific convention).
2. Otherwise, all files in the directory are candidates.
3. Among candidates, the file with the **most recent modification time** (`mtime`) is selected first. If its last line is empty or not valid JSON, the next most recent file is tried, and so on.

## Timestamp Field (Required for Health Monitoring)

Each JSON line should contain a timestamp field for `explogger-man` to assess data freshness. The following field names are recognized, checked in order of priority:

| Priority | Field Name         | Example                                  |
|----------|--------------------|------------------------------------------|
| 1        | `system_timestamp` | `"2026-03-13T10:01:52.324+00:00"`        |
| 2        | `timestamp`        | `"2026-03-13T10:01:52.581728+00:00"`     |
| 3        | `time`             | `"2026-03-13T10:01:52.324+00:00"`        |

- The value must be in **ISO 8601 format** (parseable by JavaScript `new Date()`).
- If no recognized timestamp field is found, `explogger-man` will show the data source as `NO TS` (warning state). The data itself is still served.
- Data is considered **stale** if the timestamp is more than **10 seconds** behind the manager's current time.

## Minimal Example

Directory: `data/sensor/`

File: `data/sensor/20260313_120000.log`

```jsonl
{"timestamp": "2026-03-13T12:00:01+00:00", "temperature": 25.3, "humidity": 60}
{"timestamp": "2026-03-13T12:00:02+00:00", "temperature": 25.4, "humidity": 59}
```

This produces the following in the `/status` API response:

```json
{
  "data": {
    "sensor": {
      "timestamp": "2026-03-13T12:00:02+00:00",
      "temperature": 25.4,
      "humidity": 59
    }
  }
}
```

And `explogger-man` will display a `SENSOR OK` or `SENSOR STALE` badge accordingly.
