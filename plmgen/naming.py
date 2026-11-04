"""File names for everything the generator delivers, in one place, so writers and readers agree.

Every delivered file says what it contains and when it is from, so it stays identifiable after being
copied into a flat folder or shown in a pipeline log:

  base extract part   <table>_<YYYYMMDD>_<NNNNN>.csv           master_part_20261103_00001.csv
  daily batch file    <feed>_<YYYYMMDD>.<ext>                  sap_pdm_cdc_20261104.jsonl
  micro-batch file    <feed>_<YYYYMMDD>_<HHMM>.<ext>           sap_pdm_cdc_20261104_0615.jsonl

For a base part the date is the as-of date of the extract. For batch files it is the day the changes
happened, and HHMM is the start of the micro-batch window. For delta extracts the feed is the table name.
"""

import re
from datetime import date

# <feed>_<YYYYMMDD>[_<HHMM>].<ext>
BATCH_FILE = re.compile(r"^(?P<feed>.+)_(?P<date>\d{8})(?:_(?P<window>\d{4}))?\.(?P<ext>csv|jsonl)$")
# <table>_<YYYYMMDD>_<NNNNN>.csv
BASE_PART = re.compile(r"^(?P<table>.+)_(?P<date>\d{8})_(?P<part>\d{5})\.csv$")

SAP_FEED = "sap_pdm_cdc"
WORKFLOW_FEED = "part_release_events"
WINDCHILL_FEED = "windchill_changelog"


def base_part_name(table, as_of, number):
    return f"{table}_{as_of:%Y%m%d}_{number:05d}.csv"


def batch_file_name(feed, day, window, ext):
    """window is "HHMM" for a micro-batch file, or empty for the single file of a day."""
    return f"{feed}_{day:%Y%m%d}{'_' + window if window else ''}.{ext}"


def parse_batch_file(name):
    """Return (feed, date, window, ext) for a batch file name, or None. window is '' for a daily file."""
    match = BATCH_FILE.match(name)
    if not match:
        return None
    day = match.group("date")
    return (match.group("feed"), date(int(day[:4]), int(day[4:6]), int(day[6:])),
            match.group("window") or "", match.group("ext"))
