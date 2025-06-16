import sqlite3
import pandas as pd
from pathlib import Path
from openpyxl.styles import PatternFill, Font
from openpyxl.styles.numbers import BUILTIN_FORMATS
from openpyxl.utils.dataframe import dataframe_to_rows
from typing import Any

# Define styles as constants for reuse
HEADER_FONT = Font(bold=True, color="FFFFFF")
HEADER_FILL = PatternFill(start_color="4F81BD", end_color="4F81BD", fill_type="solid")
YELLOW_FILL = PatternFill(start_color="FFFFA0", end_color="FFFFA0", fill_type="solid")
RED_FILL = PatternFill(start_color="FF9696", end_color="FF9696", fill_type="solid")
BLUE_FILL = PatternFill(start_color="E6E6FF", end_color="E6E6FF", fill_type="solid")
TEXT_FORMAT = BUILTIN_FORMATS[49]  # '@' (Text format)

def escape_formula(value: Any) -> Any:
    """
    Prepends a single quote to a string if it starts with a character
    that Excel might interpret as a formula, to prevent formula injection.

    :param value: The value to check and potentially escape.
    :return: The escaped string or the original value if no escape was needed.
    """
    if isinstance(value, str) and value.startswith(('=', '-', '+', '@')):
        return f"'{value}"
    return value

def export_logs_to_excel(db_path: Path, output_path: Path) -> None:
    """
    Exports log entries from the SQLite database to a styled Excel file.

    Features include auto-sized columns, conditional row coloring based on log level,
    and protection against Excel formula injection.

    :param db_path: The file path to the SQLite database.
    :param output_path: The file path where the Excel file will be saved.
    """
    if not db_path.exists():
        print(f"Error: Database file not found at '{db_path}'")
        return

    print(f"Connecting to database: {db_path}")
    try:
        with sqlite3.connect(db_path) as con:
            query = """
            SELECT 
                datetime(timestamp, 'unixepoch', 'localtime') as Timestamp,
                level as 'Log Level',
                module as 'Module',
                funcName || ':' || lineno as 'Func Source',
                message as 'Message'
            FROM logs
            ORDER BY timestamp ASC;
            """
            df = pd.read_sql_query(query, con)
            print(f"Read {len(df)} log entries from the database.")
    except (sqlite3.Error, pd.errors.DatabaseError) as e:
        print(f"An error occurred while reading the database: {e}")
        return

    if df.empty:
        print("No log entries to export.")
        return

    # Sanitize data to prevent formula injection
    print("Sanitizing data to prevent Excel formula errors...")
    for col in ['Module', 'Func Source', 'Message']:
        df[col] = df[col].apply(escape_formula)

    print(f"Writing data to Excel file: {output_path}")
    try:
        # Use openpyxl directly for more control over styling
        with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Logs')
            ws = writer.sheets['Logs']

            # Style header
            for cell in ws[1]:
                cell.font = HEADER_FONT
                cell.fill = HEADER_FILL

            # Style rows and set data types
            level_col_idx = df.columns.get_loc('Log Level') + 1
            for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
                log_level = row[level_col_idx - 1].value
                fill_map = {
                    'WARNING': YELLOW_FILL,
                    'ERROR': RED_FILL,
                    'CRITICAL': RED_FILL,
                    'FATAL': RED_FILL,
                    'DEBUG': BLUE_FILL
                }
                fill_to_apply = fill_map.get(log_level)
                if fill_to_apply:
                    for cell in row:
                        cell.fill = fill_to_apply
                
                # Apply text format to all but the timestamp column
                for cell in row[1:]:
                    cell.number_format = TEXT_FORMAT

            # Auto-adjust column widths
            column_widths = {}
            for row in ws.iter_rows():
                for i, cell in enumerate(row):
                    if cell.value:
                        column_widths[i] = max(column_widths.get(i, 0), len(str(cell.value)))
            
            for i, width in column_widths.items():
                ws.column_dimensions[ws.cell(row=1, column=i + 1).column_letter].width = width + 2

        print("\nExport successful!")
        print(f"File saved to: {output_path.resolve()}")
    except Exception as e:
        print(f"An error occurred while writing or styling the Excel file: {e}")
