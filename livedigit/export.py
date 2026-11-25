"""Spreadsheet-safe exports of the visible lead collection."""

from io import BytesIO

import pandas as pd
from openpyxl.utils import get_column_letter

from livedigit.store import FIELDS


def table(rows):
    return pd.DataFrame(rows, columns=list(FIELDS)).rename(columns=FIELDS).fillna("")


def csv_bytes(frame):
    # Prevent spreadsheet formula evaluation. Prefix phones too, to retain +91.
    safe = frame.map(lambda value: "'" + value if isinstance(value, str) and value
                     and (value.lstrip().startswith(("=", "+", "-", "@")) or
                          value.startswith(("\t", "\n", "\r"))) else value)
    return safe.to_csv(index=False).encode("utf-8-sig")


def excel_bytes(frame):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="Business leads")
        sheet = writer.sheets["Business leads"]
        sheet.freeze_panes = "B2"
        sheet.auto_filter.ref = sheet.dimensions
        for index, header in enumerate(frame.columns, 1):
            sheet.column_dimensions[get_column_letter(index)].width = min(42, max(16, len(header) + 3))
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                if isinstance(cell.value, str):
                    cell.data_type = "s"
                    cell.number_format = "@"
    return output.getvalue()
